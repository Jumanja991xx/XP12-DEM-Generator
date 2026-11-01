"""Remote SLPK (I3S IntegratedMesh) reader: fetches single files via HTTP range requests."""
import gzip, json, os, pickle, struct, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import requests

ROOT = os.environ.get("DOMMESH_WORK", os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))


class Slpk:
    def __init__(self, url, key):
        self.url, self.key = url, key
        self.s = requests.Session()
        self.cache = os.path.join(ROOT, "cache", key)
        os.makedirs(self.cache, exist_ok=True)
        idx = os.path.join(self.cache, "_idx.pkl")
        if not os.path.exists(idx):
            pickle.dump(self._read_index(), open(idx, "wb"))
        self.idx = pickle.load(open(idx, "rb"))
        self.pages = {}

    def _range(self, a, b):
        last = None
        for attempt in range(12):
            url = self.url if attempt % 2 == 0 else self.url.replace("download1.", "download2.")
            try:
                r = self.s.get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=300)
                last = r.status_code
                if r.status_code == 206 and len(r.content) == b - a + 1:
                    return r.content
            except Exception:
                pass
            time.sleep(min(30, 2 * (attempt + 1)))
        raise IOError(f"range {a}-{b} failed (last status {last})")

    def _read_index(self):
        size = int(self.s.head(self.url, timeout=60).headers["Content-Length"])
        tail = self._range(size - 65536, size - 1)
        j = tail.rfind(b"PK\x06\x06")
        cd_size, cd_off = struct.unpack("<QQ", tail[j + 40:j + 56])
        d = self._range(cd_off, cd_off + cd_size - 1)
        i, idx = 0, {}
        while d[i:i + 4] == b"PK\x01\x02":
            (csize, usize, nl, el, cl) = struct.unpack("<IIHHH", d[i + 20:i + 34])
            off = struct.unpack("<I", d[i + 42:i + 46])[0]
            name = d[i + 46:i + 46 + nl].decode()
            extra = d[i + 46 + nl:i + 46 + nl + el]
            j = 0
            while j + 4 <= len(extra):
                hid, hs = struct.unpack("<HH", extra[j:j + 4])
                body, k = extra[j + 4:j + 4 + hs], 0
                if hid == 1:
                    if usize == 0xFFFFFFFF:
                        k += 8
                    if csize == 0xFFFFFFFF:
                        csize = struct.unpack("<Q", body[k:k + 8])[0]; k += 8
                    if off == 0xFFFFFFFF:
                        off = struct.unpack("<Q", body[k:k + 8])[0]
                j += 4 + hs
            idx[name] = (off, csize)
            i += 46 + nl + el + cl
        return idx

    def raw(self, name):
        path = os.path.join(self.cache, name.replace("/", "_"))
        if os.path.exists(path):
            return open(path, "rb").read()
        off, csize = self.idx[name]
        head = self._range(off, off + 29)
        assert head[:4] == b"PK\x03\x04"
        nl, el = struct.unpack("<HH", head[26:30])
        data = self._range(off + 30 + nl + el, off + 30 + nl + el + csize - 1)
        open(path + ".part", "wb").write(data)
        os.replace(path + ".part", path)
        return data

    def get(self, name):
        data = self.raw(name)
        return gzip.decompress(data) if name.endswith(".gz") else data

    def fetch(self, names, workers=8):
        with ThreadPoolExecutor(workers) as p:
            list(p.map(self.raw, [n for n in names if n in self.idx]))

    # --- node tree ---
    def node(self, i):
        p = i // 64
        if p not in self.pages:
            self.pages[p] = json.loads(self.get(f"nodepages/{p}.json.gz"))["nodes"]
        return self.pages[p][i % 64]

    @staticmethod
    def extent(n):
        x, y, z, w = n["obb"]["quaternion"]
        R = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                      [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                      [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
        return np.array(n["obb"]["center"]), np.abs(R) @ np.array(n["obb"]["halfSize"])

    def select(self, x0, y0, x1, y1, max_half):
        out, stack = [], [0]
        while stack:
            i = stack.pop()
            n = self.node(i)
            c, e = self.extent(n)
            if c[0] + e[0] < x0 or c[0] - e[0] > x1 or c[1] + e[1] < y0 or c[1] - e[1] > y1:
                continue
            kids = n.get("children", [])
            if (max(e[0], e[1]) <= max_half or not kids) and "mesh" in n:
                out.append(i)
            else:
                stack.extend(kids)
        return out

    def geometry(self, i):
        """Absolute UTM32 positions (N x 3, per triangle vertex) and uv (N x 2)."""
        n = self.node(i)
        r = n["mesh"]["geometry"]["resource"]
        g = self.get(f"nodes/{r}/geometries/0.bin.gz")
        vc = struct.unpack("<I", g[:4])[0]
        pos = np.frombuffer(g, np.float32, vc * 3, 8).reshape(-1, 3).astype(np.float64) + n["obb"]["center"]
        uv = np.frombuffer(g, np.float32, vc * 2, 8 + vc * 12).reshape(-1, 2)
        return pos, uv, r
