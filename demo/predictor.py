# Copyright (c) Facebook, Inc. and its affiliates.
# Copied from: https://github.com/facebookresearch/detectron2/blob/master/demo/predictor.py
import atexit
import bisect
import multiprocessing as mp
from collections import deque
from pycocotools.mask import encode

import cv2
import torch
import numpy as np
from tqdm import tqdm

from detectron2.data import MetadataCatalog
from detectron2.engine.defaults import DefaultPredictor
from detectron2.utils.video_visualizer import VideoVisualizer
from detectron2.utils.visualizer import ColorMode, Visualizer
from detectron2.structures.instances import Instances
from detectron2.structures.boxes import Boxes


def move_tensors_to_cpu(obj):
    """Recursively move all tensors in the object to CPU."""
    if torch.is_tensor(obj):
        return obj.cpu()
    elif hasattr(obj, "__dict__"):
        for k, v in vars(obj).items():
            setattr(obj, k, move_tensors_to_cpu(v))
    elif isinstance(obj, (list, tuple, set)):
        return type(obj)(move_tensors_to_cpu(item) for item in obj)
    elif isinstance(obj, dict):
        return {k: move_tensors_to_cpu(v) for k, v in obj.items()}
    return obj


class VisualizationDemo(object):
    def __init__(self, cfg, instance_mode=ColorMode.IMAGE, parallel=False):
        """
        Args:
            cfg (CfgNode):
            instance_mode (ColorMode):
            parallel (bool): whether to run the model in different processes from visualization.
                Useful since the visualization logic can be slow.
        """
        self.metadata = MetadataCatalog.get(cfg.DATASETS.TEST[0] if len(cfg.DATASETS.TEST) else "__unused")
        self.cpu_device = torch.device("cpu")
        self.instance_mode = instance_mode

        self.parallel = parallel
        if parallel:
            num_gpu = torch.cuda.device_count()
            self.predictor = AsyncPredictor(cfg, num_gpus=num_gpu)
        else:
            self.predictor = DefaultPredictor(cfg)

    def run_on_image(self, image, class_filter=None, not_empty_threshold=10):
        """
        Args:
            image (np.ndarray): an image of shape (H, W, C) (in BGR order).
                This is the format used by OpenCV.
        Returns:
            predictions (dict): the output of the model.
            vis_output (VisImage): the visualized image output.
        """
        vis_output = None
        with torch.no_grad():
            predictions = self.predictor(image)

        predictions = move_tensors_to_cpu(predictions)

        if len(predictions["instances"]) == 0:
            return None, None

        # Convert image from OpenCV BGR format to Matplotlib RGB format.
        image = image[:, :, ::-1]
        visualizer = Visualizer(image, self.metadata, instance_mode=self.instance_mode)
        if "panoptic_seg" in predictions:
            panoptic_seg, segments_info = predictions["panoptic_seg"]
            vis_output = visualizer.draw_panoptic_seg_predictions(panoptic_seg.to(self.cpu_device), segments_info)
        else:
            if "sem_seg" in predictions:
                vis_output = visualizer.draw_sem_seg(predictions["sem_seg"].argmax(dim=0).to(self.cpu_device))
            if "instances" in predictions:
                instances = predictions["instances"]

                if not_empty_threshold:
                    boxes: Boxes = instances.get("pred_boxes").to(self.cpu_device)

                    idxs = [i for i, keep in enumerate(boxes.nonempty(threshold=not_empty_threshold)) if keep]

                    if len(idxs) == 0:
                        return None, None

                    instances = Instances.cat([instances[i] for i in idxs])

                if class_filter is not None:
                    classes = instances.get("pred_classes").to(self.cpu_device)
                    scores = instances.get("scores").to(self.cpu_device)

                    idxs = [
                        i
                        for i, c in enumerate(classes)
                        if c.item() in class_filter.keys() and scores[i] > class_filter[c.item()]
                    ]

                    if len(idxs) == 0:
                        return None, None

                    instances = Instances.cat([instances[i] for i in idxs])

                predictions["instances"] = instances

                if instances is not None:
                    vis_output = visualizer.draw_instance_predictions(predictions=instances.to(self.cpu_device))

        return predictions, vis_output

    def _frame_from_video(self, video):
        while video.isOpened():
            success, frame = video.read()
            if success:
                yield frame
            else:
                break

    def run_on_video(self, video):
        """
        Visualizes predictions on frames of the input video.
        Args:
            video (cv2.VideoCapture): a :class:`VideoCapture` object, whose source can be
                either a webcam or a video file.
        Yields:
            ndarray: BGR visualizations of each video frame.
        """
        video_visualizer = VideoVisualizer(self.metadata, self.instance_mode)

        def process_predictions(frame, predictions):
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if "panoptic_seg" in predictions:
                panoptic_seg, segments_info = predictions["panoptic_seg"]
                vis_frame = video_visualizer.draw_panoptic_seg_predictions(
                    frame, panoptic_seg.to(self.cpu_device), segments_info
                )
            elif "instances" in predictions:
                predictions = predictions["instances"].to(self.cpu_device)
                vis_frame = video_visualizer.draw_instance_predictions(frame, predictions)
            elif "sem_seg" in predictions:
                vis_frame = video_visualizer.draw_sem_seg(
                    frame, predictions["sem_seg"].argmax(dim=0).to(self.cpu_device)
                )

            # Converts Matplotlib RGB format to OpenCV BGR format
            vis_frame = cv2.cvtColor(vis_frame.get_image(), cv2.COLOR_RGB2BGR)
            return vis_frame

        frame_gen = self._frame_from_video(video)
        if self.parallel:
            buffer_size = self.predictor.default_buffer_size

            frame_data = deque()

            for cnt, frame in enumerate(frame_gen):
                frame_data.append(frame)
                self.predictor.put(frame)

                if cnt >= buffer_size:
                    frame = frame_data.popleft()
                    predictions = self.predictor.get()
                    yield process_predictions(frame, predictions)

            while len(frame_data):
                frame = frame_data.popleft()
                predictions = self.predictor.get()
                yield process_predictions(frame, predictions)
        else:
            for frame in frame_gen:
                yield process_predictions(frame, self.predictor(frame))

    def rle_encode(self, mask):
        """Encodes a mask in RLE format."""
        mask = np.asfortranarray(mask.astype(np.uint8))

        return encode(mask)

    def process_frame(
        self,
        frame,
        predictions,
        masks_info_dict,
        not_empty_threshold=5,
        class_filter=None,
        device=None,
        raw_mask_path=None,
    ):
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rle_encoded_frame = []
        raw_masks_frame = []

        if "instances" in predictions:
            instances: Instances = predictions["instances"]
            boxes: Boxes = instances.get("pred_boxes").to("cpu")
            classes = instances.get("pred_classes").to("cpu")
            scores = instances.get("scores").to("cpu")

            idxs = list(range(len(boxes)))

            if not_empty_threshold:
                idxs = [
                    i for i in idxs if boxes[i].nonempty(threshold=not_empty_threshold)
                ]

            if class_filter is not None:
                idxs = [
                    i
                    for i in idxs
                    if classes[i].item() in class_filter
                    and scores[i] > class_filter[classes[i].item()]
                ]

            instances = Instances.cat([instances[i] for i in idxs])
            boxes: Boxes = instances.get("pred_boxes").to("cpu")
            classes = instances.get("pred_classes").to("cpu")
            scores = instances.get("scores").to("cpu")

            masks = instances.get("pred_masks")

            raw_masks_frame = masks.to("cpu").numpy()

            for mask in raw_masks_frame:
                rle_encoded_frame.append(self.rle_encode(mask))

        else:
            raise ValueError

        masks_info_dict["rle_masks"].append(rle_encoded_frame)
        masks_info_dict["classes"].append(classes.to("cpu").numpy())
        masks_info_dict["boxes"].append(boxes.tensor.to("cpu").numpy())
        masks_info_dict["scores"].append(scores.to("cpu").numpy())

    def run_on_video_rle(
        self, video, size=None, class_filter=None, not_empty_threshold=5
    ):
        print(f"Num Threads: {torch.get_num_threads()}")
        masks_info_dict = {
            "rle_masks": [],
            "raw_masks": [],
            "classes": [],
            "boxes": [],
            "scores": [],
        }

        video_capture = cv2.VideoCapture(video)
        num_frames = int(video_capture.get(cv2.CAP_PROP_FRAME_COUNT))

        with tqdm(total=num_frames, desc="  MaskDINO") as pbar:
            for cnt in range(num_frames):
                ret, frame = video_capture.read()
                if not ret:
                    break

                if size is not None:
                    frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)

                predictions = self.predictor(frame)
                self.process_frame(
                    frame,
                    predictions,
                    masks_info_dict,
                    not_empty_threshold,
                    class_filter,
                )

                pbar.update(1)

        video_capture.release()

        return masks_info_dict


class AsyncPredictor:
    """
    A predictor that runs the model asynchronously, possibly on >1 GPUs.
    Because rendering the visualization takes considerably amount of time,
    this helps improve throughput a little bit when rendering videos.
    """

    class _StopToken:
        pass

    class _PredictWorker(mp.Process):
        def __init__(self, cfg, task_queue, result_queue):
            self.cfg = cfg
            self.task_queue = task_queue
            self.result_queue = result_queue
            super().__init__()

        def run(self):
            predictor = DefaultPredictor(self.cfg)

            while True:
                task = self.task_queue.get()
                if isinstance(task, AsyncPredictor._StopToken):
                    break
                idx, data = task
                result = predictor(data)
                self.result_queue.put((idx, result))

    def __init__(self, cfg, num_gpus: int = 1):
        """
        Args:
            cfg (CfgNode):
            num_gpus (int): if 0, will run on CPU
        """
        num_workers = max(num_gpus, 1)
        self.task_queue = mp.Queue(maxsize=num_workers * 3)
        self.result_queue = mp.Queue(maxsize=num_workers * 3)
        self.procs = []
        for gpuid in range(max(num_gpus, 1)):
            cfg = cfg.clone()
            cfg.defrost()
            cfg.MODEL.DEVICE = "cuda:{}".format(gpuid) if num_gpus > 0 else "cpu"
            self.procs.append(AsyncPredictor._PredictWorker(cfg, self.task_queue, self.result_queue))

        self.put_idx = 0
        self.get_idx = 0
        self.result_rank = []
        self.result_data = []

        for p in self.procs:
            p.start()
        atexit.register(self.shutdown)

    def put(self, image):
        self.put_idx += 1
        self.task_queue.put((self.put_idx, image))

    def get(self):
        self.get_idx += 1  # the index needed for this request
        if len(self.result_rank) and self.result_rank[0] == self.get_idx:
            res = self.result_data[0]
            del self.result_data[0], self.result_rank[0]
            return res

        while True:
            # make sure the results are returned in the correct order
            idx, res = self.result_queue.get()
            if idx == self.get_idx:
                return res
            insert = bisect.bisect(self.result_rank, idx)
            self.result_rank.insert(insert, idx)
            self.result_data.insert(insert, res)

    def __len__(self):
        return self.put_idx - self.get_idx

    def __call__(self, image):
        self.put(image)
        return self.get()

    def shutdown(self):
        for _ in self.procs:
            self.task_queue.put(AsyncPredictor._StopToken())

    @property
    def default_buffer_size(self):
        return len(self.procs) * 5
