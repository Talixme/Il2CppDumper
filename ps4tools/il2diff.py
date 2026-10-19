"""Compare two memory dumps of the same game session and turn the changed values into il2live paths.

    python il2diff.py <dumpdir> dump_mana_50.json dump_mana_77.json --eq 50 77
    python il2diff.py <dumpdir> a.json b.json --eq 50 77 --depth 10 --paths 3

Both dumps come from ps4dump.py in ONE game session (object addresses must match), <dumpdir>/sdk/dump.cs from
Il2CppDumper. --eq A B keeps the 32-bit ints and floats that are A in the first dump and B in the second.
For every hit the owner object is found through its klass pointer, then the references to that object are followed
backwards (fields, array elements, List<T>, Dictionary<K,V> entries) until a static field block is reached.
Output per line: path<TAB>type<TAB>value in a<TAB>value in b, verified by resolving the path in both dumps.
"""
import argparse, os, struct, sys
import numpy as np
import il2live
from il2live import KLASS_STATICS, PRIM, split_generic

NOISE_NS = ("System", "UnityEngine", "TMPro", "Unity.", "Cinemachine")
NOISE_NAMES = ("StatChangedArgs", "Action", "Delegate", "MulticastDelegate", "Func", "EventHandler")


class Diff:
    def __init__(self, dumpdir, ja, jb, depth, max_parents):
        self.a, self.b = il2live.Dump(dumpdir, ja), il2live.Dump(dumpdir, jb)
        self.types = il2live.parse_dump_cs(os.path.join(dumpdir, "sdk", "dump.cs"))
        self.rt = il2live.Runtime(self.a, self.types)
        self.res = il2live.Resolver(self.rt)
        self.depth, self.max_parents = depth, max_parents

        async def aread(va, n):
            return self.a.read(va, n)
        self.aread = aread
        self.klass_arr = self.rt.klass_arr
        # static field blocks: start -> (Type, size)
        self.statics = {}
        for k in self.rt.klasses:
            sf = self.a.u64(k + KLASS_STATICS)
            t = self.rt.type_of_klass(k)
            if sf and t and t.statics:
                self.statics[sf] = (t, max(t.statics) + 8)
        self.static_starts = np.array(sorted(self.statics), dtype=np.uint64)

    # ---- value scan ---------------------------------------------------------------------------------------------------
    def scan(self, va, vb):
        byb = {(r["start"], r["end"]): r for r in self.b.regs}
        hits = []
        for r in self.a.regs:
            rb = byb.get((r["start"], r["end"]))
            if not rb:
                continue
            n = (r["end"] - r["start"]) // 4
            for dtype, fa, fb in ((np.int32, int(va), int(vb)), (np.float32, float(va), float(vb))):
                if dtype is np.int32 and (fa != va or fb != vb):
                    continue
                x = np.frombuffer(self.a.m, dtype=dtype, count=n, offset=r["offset"])
                y = np.frombuffer(self.b.m, dtype=dtype, count=n, offset=rb["offset"])
                for i in np.nonzero((x == fa) & (y == fb))[0]:
                    hits.append((r["start"] + int(i) * 4, "int" if dtype is np.int32 else "float"))
        return hits

    # ---- object structure ---------------------------------------------------------------------------------------------
    def owner(self, va, maxback=0x100000):
        """(object start, klass) of the object containing va: nearest preceding qword that is an Il2CppClass pointer."""
        d = self.a
        o = d.off(va)
        if o is None:
            return None
        i = np.searchsorted(d.starts, va, side="right") - 1
        lo = max(int(d.offs[i]), o - maxback)
        lo += (o - lo) % 8
        a = np.frombuffer(d.m, dtype=np.uint64, count=(o - lo) // 8 + 1, offset=lo)
        idx = np.nonzero(np.isin(a, self.klass_arr))[0]
        if len(idx) == 0:
            return None
        start = va - (len(a) - 1 - int(idx[-1])) * 8
        return start, d.u64(start)

    def refs(self, target):
        pat = np.uint64(target)
        out = []
        for r, a in self.a.arrays():
            for idx in np.nonzero(a == pat)[0]:
                out.append(r["start"] + int(idx) * 8)
        return out

    def static_root(self, va):
        i = np.searchsorted(self.static_starts, va, side="right") - 1
        if i < 0:
            return None
        start = int(self.static_starts[i])
        t, size = self.statics[start]
        if va >= start + size:
            return None
        off = va - start
        name = next((fn for o, (_, fn) in t.statics.items() if o == off), None)
        return f"{t.full}::{name}" if name else None

    def field_name(self, t, off):
        c = t
        while c:
            if off in c.fields:
                return c.fields[off][1], c.fields[off][0]
            c = self.res.find_type(c.bases[0], c.ns) if c.bases and c.bases[0] else None
        return None, None

    async def step_up(self, va):
        """What holds the pointer/value at va: ('static', path) | ('obj', object start, suffix piece) | None.
        Dictionary entries and List buffers are collapsed into {key} / [i] on the owning container."""
        root = self.static_root(va)
        if root:
            return ("static", root)
        ow = self.owner(va)
        if not ow:
            return None
        start, k = ow
        name = self.rt.klasses.get(k, "")
        if name.endswith("[]"):                                  # Il2CppArray, elements from 0x20
            elem = name[:-2]
            length = self.a.u64(start + 0x18) or 0
            if elem == "Entry":                                  # Dictionary<K,V>.Entry[]: {hash, next, key@8, value@0x10}
                i, rest = divmod(va - start - 0x20, 0x18)
                if rest != 0x10 or i >= length:
                    return None
                dicts = [r for r in self.refs(start) if (o := self.owner(r)) and r - o[0] == 0x18]
                if not dicts:
                    return None
                d = self.owner(dicts[0])[0]
                kt = self.rt.type_of_klass(self.a.u64(d))
                if kt and kt.name.startswith("Dictionary"):
                    names = await il2live.dict_key_names(self.res, self.rt, self.aread, self.dict_key_type(d), start + 0x20 + i * 0x18)
                    return ("obj", d, f"{{{names[0] if names else '#%d' % i}}}")
                return None
            es = il2live.elem_size(self.res, elem)
            i, rest = divmod(va - start - 0x20, es)
            if i >= length:
                return None
            piece = f"[{i}]"
            if rest:
                t = self.res.find_type(elem)
                fn, _ = self.field_name(t, rest) if t else (None, None)
                if not fn:
                    return None
                piece += f".{fn}"
            lists = [r for r in self.refs(start) if (o := self.owner(r)) and r - o[0] == 0x10]
            for r in lists:                                      # List<T>._items -> report on the List object
                lst = self.owner(r)[0]
                lt = self.rt.type_of_klass(self.a.u64(lst))
                if lt and lt.name.startswith("List"):
                    return ("obj", lst, piece)
            return ("obj", start, piece)
        t = self.rt.type_of_klass(k)
        if not t:
            return None
        fn, _ = self.field_name(t, va - start)
        if not fn:
            return None
        return ("obj", start, f".{fn}")

    def dict_key_type(self, d):
        """Key type of a Dictionary object: whoever points at it tells us its declared field type."""
        for r in self.refs(d):
            ow = self.owner(r)
            if ow:
                t = self.rt.type_of_klass(ow[1])
                if t:
                    _, ft = self.field_name(t, r - ow[0])
                    if ft and ft.startswith("Dictionary<"):
                        return split_generic(ft)[1][0]
        return "int"

    async def paths_to(self, va, max_paths):
        """Static-rooted paths that reach the value at va, shortest first."""
        done, out = [], []
        frontier = [(va, "")]
        seen = set()
        for level in range(self.depth):
            nxt = []
            for addr, suffix in frontier:
                s = await self.step_up(addr)
                if not s:
                    continue
                if s[0] == "static":
                    out.append(s[1] + suffix)
                    if len(out) >= max_paths:
                        return out
                    continue
                _, obj, piece = s
                if obj in seen:
                    continue
                seen.add(obj)
                t = self.rt.type_of_klass(self.a.u64(obj))
                if t and (t.ns.startswith(NOISE_NS) and not t.name.startswith(("List", "Dictionary", "Entry")) or t.name in NOISE_NAMES):
                    continue
                for r in self.refs(obj)[:self.max_parents]:
                    nxt.append((r, piece + suffix))
            frontier = nxt
            if not frontier:
                break
        return out

    async def verify(self, path):
        vals = []
        for d in (self.a, self.b):
            async def read(va, n, d=d):
                return d.read(va, n)
            try:
                addr, ftype = await il2live.follow(self.res, self.rt, read, self.rt.base, path)
                if addr is None:
                    return None
                vals.append((await il2live.read_value(self.res, read, addr, ftype), ftype))
            except SystemExit:
                return None
        return vals


async def amain(a):
    diff = Diff(a.dumpdir, a.a, a.b, a.depth, a.parents)
    hits = diff.scan(a.eq[0], a.eq[1])
    print(f"{len(hits)} locations are {a.eq[0]} in {a.a} and {a.eq[1]} in {a.b}", file=sys.stderr)
    for va, kind in hits[:a.hits]:
        paths = await diff.paths_to(va, a.paths)
        if not paths:
            print(f"# {va:#x} ({kind}): no static root within {a.depth} hops", file=sys.stderr)
        for p in paths:
            v = await diff.verify(p)
            if v:
                print(f"{p}\t{v[0][1]}\t{v[0][0]}\t{v[1][0]}")
            else:
                print(f"# {p}: does not resolve", file=sys.stderr)


if __name__ == "__main__":
    import asyncio
    ap = argparse.ArgumentParser()
    ap.add_argument("dumpdir")
    ap.add_argument("a", help="first dump json (e.g. dump_mana_50.json)")
    ap.add_argument("b", help="second dump json")
    ap.add_argument("--eq", nargs=2, required=True, metavar=("A", "B"), help="value in the first and in the second dump")
    ap.add_argument("--depth", type=int, default=10, help="reference hops from the value to a static field (default 10)")
    ap.add_argument("--paths", type=int, default=3, help="paths to report per hit (default 3)")
    ap.add_argument("--parents", type=int, default=6, help="references followed per object (default 6)")
    ap.add_argument("--hits", type=int, default=20, help="hits to trace (default 20)")
    args = ap.parse_args()
    asyncio.run(amain(args))
