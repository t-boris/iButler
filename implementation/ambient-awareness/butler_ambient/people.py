"""Local COCO person detection using OpenCV Zoo's YOLOX ONNX model.

Model and decoding reference: opencv/opencv_zoo/models/object_detection_yolox
(Apache-2.0). No images or detection output are retained by this module.
"""
from __future__ import annotations


class PeopleDetector:
    def __init__(self, model):
        import cv2
        import numpy as np
        self.cv, self.np = cv2, np
        self.net = cv2.dnn.readNet(str(model))
        coordinates, scales = [], []
        for stride in (8, 16, 32):
            yy, xx = np.indices((640 // stride, 640 // stride))
            coordinates.append(np.column_stack((xx.ravel(), yy.ravel())))
            scales.append(np.full((xx.size, 1), stride))
        self.grid = np.concatenate(coordinates)
        self.scales = np.concatenate(scales)

    def detect(self, image):
        cv, np = self.cv, self.np
        height, width = image.shape[:2]
        ratio = min(640 / width, 640 / height)
        canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
        resized = cv.resize(image, (int(width * ratio), int(height * ratio)))
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        self.net.setInput(cv.dnn.blobFromImage(canvas, swapRB=True))
        predictions = self.net.forward()[0]
        centers = (predictions[:, :2] + self.grid) * self.scales
        sizes = np.exp(predictions[:, 2:4]) * self.scales
        probabilities = predictions[:, 4:5] * predictions[:, 5:]
        classes = probabilities.argmax(axis=1)
        selected = (classes == 0) & (probabilities[:, 0] >= .5)
        boxes = np.column_stack((centers[selected] - sizes[selected] / 2, sizes[selected])) / ratio
        scores = probabilities[selected, 0]
        keep = cv.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), .5, .5)
        return [boxes[int(i)].tolist() for i in np.asarray(keep).flatten()]
