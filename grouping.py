"""K-means image grouping (from groupimg.py), usable as a library."""

import math
import os
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image
from tqdm import tqdm

Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore")


class KMeansGrouper:
    def __init__(self, k=5, size=False, resample=128):
        self.k = k
        self.cluster = []
        self.data = []
        self.end = []
        self._i = 0
        self.size = size
        self.resample = resample
        self._pbar = None

    def manhattan_distance(self, x1, x2):
        return sum(abs(float(x1[i]) - float(x2[i])) for i in range(len(x1)))

    def read_image(self, path):
        if self._i >= self.k:
            self._i = 0
        try:
            with Image.open(path) as img:
                osize = img.size
                thumb = img.copy()
                thumb.thumbnail((self.resample, self.resample))
                tw, th = thumb.size
                hist = np.histogram(np.asarray(thumb))[0]
            v = [float(p) / float(max(tw * th, 1)) * 100 for p in hist]
            if self.size:
                v += [osize[0], osize[1]]
            if self._pbar:
                self._pbar.update(1)
            idx = self._i
            self._i += 1
            return [idx, v, path]
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return [None, None, None]

    def generate_k_means(self):
        final_mean = []
        for c in range(self.k):
            partial_mean = []
            for i in range(len(self.data[0])):
                s = 0.0
                t = 0
                for j in range(len(self.data)):
                    if self.cluster[j] == c:
                        s += self.data[j][i]
                        t += 1
                partial_mean.append(float(s) / float(t) if t else float("inf"))
            final_mean.append(partial_mean)
        return final_mean

    def fit(self, image_paths: list[str], show_progress: bool = True) -> list[int]:
        """Cluster images; returns cluster id per path (same order as input)."""
        if not image_paths:
            return []

        self._i = 0
        self._pbar = tqdm(total=len(image_paths), disable=not show_progress)
        workers = min(8, len(image_paths), os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            result = list(executor.map(self.read_image, image_paths))
        self._pbar.close()
        self._pbar = None

        self.cluster = [r[0] for r in result if r[0] is not None]
        self.data = [r[1] for r in result if r[1] is not None]
        self.end = [r[2] for r in result if r[2] is not None]

        self._rearrange_clusters()
        path_to_cluster = {path: cid for path, cid in zip(self.end, self.cluster)}
        return [path_to_cluster.get(p, 0) for p in image_paths]

    def _rearrange_clusters(self):
        isover = False
        while not isover:
            isover = True
            m = self.generate_k_means()
            for x in range(len(self.cluster)):
                dist = [self.manhattan_distance(self.data[x], m[a]) for a in range(self.k)]
                best = dist.index(min(dist))
                if self.cluster[x] != best:
                    self.cluster[x] = best
                    isover = False


def optimal_k(n_items: int, requested: int | None = None) -> int:
    if requested and requested > 0:
        return min(requested, max(1, n_items))
    if n_items <= 1:
        return 1
    return min(max(2, int(math.sqrt(n_items))), n_items, 20)
