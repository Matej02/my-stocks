"""Falešné KV (Upstash) v paměti – stejné příkazy, jaké backend používá. Sdílí testy."""
import json
store = {}
def kv(*a):
    op, k = a[0], (a[1] if len(a) > 1 else None)
    if op == "GET": return store.get(k)
    if op == "SET": store[k] = a[2]; return "OK"
    if op == "HSET":
        h = store.setdefault(k, {})
        for i in range(2, len(a) - 1, 2): h[a[i]] = a[i + 1]
        return 1
    if op == "HGETALL":
        out = []
        for f, v in (store.get(k) or {}).items(): out += [f, v]
        return out
    if op == "RPUSH": store.setdefault(k, []).append(a[2]); return 1
    if op == "LRANGE": return list(store.get(k) or [])
    if op == "SADD": store.setdefault(k, set()).add(a[2]); return 1
    if op == "SMEMBERS": return list(store.get(k) or [])
    if op == "INCR": store[k] = int(store.get(k) or 0) + 1; return store[k]
    return 1
