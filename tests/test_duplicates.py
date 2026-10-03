"""重复上传处理的端到端测试：存储层策略 + HTTP 预检/确认 + 引用计数 + 旧数据迁移。

运行：python tests/test_duplicates.py
用临时目录隔离数据，不污染 data/。
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from server import config  # noqa: E402
from server.image_store import ImageStore  # noqa: E402


def _png_bytes(color=(120, 60, 200), w=80, h=60):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class TempData:
    """把 config 的数据目录指到临时目录（须在导入 ImageStore 前完成）。"""
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="dup-test-")
        self._paths = {}
        for name in ("DATA_DIR", "IMAGES_DIR", "RESULTS_DIR", "THUMBS_DIR",
                     "CACHE_DIR", "META_DIR", "IMAGES_JSON", "PIPELINES_JSON",
                     "HISTORY_JSON", "PRESETS_JSON", "QUEUE_JSON", "CACHE_JSON"):
            self._paths[name] = getattr(config, name)
        config.DATA_DIR = self.tmp
        config.IMAGES_DIR = os.path.join(self.tmp, "images")
        config.RESULTS_DIR = os.path.join(self.tmp, "results")
        config.THUMBS_DIR = os.path.join(self.tmp, "thumbnails")
        config.CACHE_DIR = os.path.join(self.tmp, "cache")
        config.META_DIR = os.path.join(self.tmp, "metadata")
        config.IMAGES_JSON = os.path.join(config.META_DIR, "images.json")
        config.PIPELINES_JSON = os.path.join(config.META_DIR, "pipelines.json")
        config.HISTORY_JSON = os.path.join(config.META_DIR, "history.json")
        config.PRESETS_JSON = os.path.join(config.META_DIR, "presets.json")
        config.QUEUE_JSON = os.path.join(config.META_DIR, "queue.json")
        config.CACHE_JSON = os.path.join(config.META_DIR, "cache.json")
        for d in (config.DATA_DIR, config.IMAGES_DIR, config.RESULTS_DIR,
                  config.THUMBS_DIR, config.CACHE_DIR, config.META_DIR):
            os.makedirs(d, exist_ok=True)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def test_store_policies():
    print("== 存储层：ask/keep/merge/merge_new ==")
    store = ImageStore()
    data = _png_bytes()

    r1, s1 = store.save_upload(data, "first.png")
    check(s1 == "created", "首次上传 -> created")

    r2, s2 = store.save_upload(data, "second.png")  # 默认 ask
    check(s2 == "duplicate", "默认 ask：同内容 -> duplicate，不改动")
    check(store.get(r1["id"])["filename"] == "first.png", "ask 不吞掉旧名字")
    check(len(store.list_records()) == 1, "ask 不产生新记录")

    r3, s3 = store.save_upload(data, "second.png", policy="keep")
    check(s3 == "created", "keep：生成独立记录")
    check(r3["id"] != r1["id"], "新记录有独立 id")
    check(r3["filename"] == "second.png", "新记录保留新名字")
    check(r3["stored_name"] == r1["stored_name"], "两条记录共享同一物理文件")
    check(store.duplicate_counts()[r1["content_hash"]] == 2, "重复计数 = 2")

    # 删一条副本不应删物理文件
    store.delete(r3["id"])
    check(os.path.exists(os.path.join(config.IMAGES_DIR, r1["stored_name"])),
          "删除副本后共享原图仍保留")
    check(store.thumbnail_path(r1["id"]) and os.path.exists(store.thumbnail_path(r1["id"])),
          "删除副本后缩略图仍保留")

    r4, s4 = store.save_upload(data, "merged-new.png", policy="merge_new")
    check(s4 == "merged_new", "merge_new：复用记录并改名")
    check(store.get(r1["id"])["filename"] == "merged-new.png", "旧记录名字被新名字覆盖")

    r5, s5 = store.save_upload(data, "ignored.png", policy="merge")
    check(s5 == "merged", "merge：复用记录但保留旧名字")
    check(store.get(r1["id"])["filename"] == "merged-new.png", "merge 不覆盖名字")

    # 删掉最后一条引用时才真正删文件
    store.delete(r1["id"])
    check(not os.path.exists(os.path.join(config.IMAGES_DIR, r1["stored_name"])),
          "最后一条记录删除后物理文件被清理")
    check(not os.path.exists(store.thumbnail_path_for(r1["content_hash"])),
          "最后一条记录删除后缩略图被清理")


def test_http():
    print("== HTTP：ask 预检 -> 弹窗确认 -> 列表重复标记 ==")
    from app import create_app
    app = create_app()
    client = app.test_client()
    data = _png_bytes(color=(10, 200, 90))

    # 首次上传（ask，无重复 -> 直接保存）
    resp = client.post("/api/images", data={"files": (io.BytesIO(data), "a.png"),
                                            "on_duplicate": "ask"},
                       content_type="multipart/form-data")
    j = resp.get_json()
    check(resp.status_code == 200 and len(j["saved"]) == 1, "首次 ask 上传直接保存")
    first_id = j["saved"][0]["id"]

    # 再次上传同内容 -> need_choice，不写入
    resp = client.post("/api/images", data={"files": (io.BytesIO(data), "b.png"),
                                            "on_duplicate": "ask"},
                       content_type="multipart/form-data")
    j = resp.get_json()
    check(j["need_choice"] is True and len(j["duplicates"]) == 1, "重复上传返回 need_choice")
    check(j["duplicates"][0]["existing"]["filename"] == "a.png", "预检给出已有记录信息")
    check(j["duplicates"][0]["index"] == 0, "预检标出文件索引 0")
    check(len(client.get("/api/images").get_json()["images"]) == 1, "确认前不产生记录")

    # 用户选择 keep 独立记录，并改名字/标签
    resp = client.post("/api/images", data={
        "files": (io.BytesIO(data), "b.png"),
        "on_duplicate": "keep",
        "on_duplicate_0": "keep",
        "filename_0": "b-renamed.png",
        "tags_0": "封面, 印刷",
    }, content_type="multipart/form-data")
    j = resp.get_json()
    check(len(j["saved"]) == 1 and j["saved"][0]["filename"] == "b-renamed.png",
          "keep + 改名：独立记录用新名字")
    check(j["saved"][0]["tags"] == ["封面", "印刷"], "keep + 新标签生效")
    check(j["saved"][0]["content_hash"], "视图带 content_hash")

    # 列表里两条记录，且都标记 duplicate_count=2
    images = client.get("/api/images").get_json()["images"]
    check(len(images) == 2, "列表有两条独立记录")
    check(all(im["duplicate_count"] == 2 for im in images), "两条记录都显示同内容 2 次")

    # 批次内重复（全新内容：同一张图在同一批里选了两次，库里没有）
    fresh = _png_bytes(color=(200, 200, 30))
    resp = client.post("/api/images", data={
        "files": [(io.BytesIO(fresh), "c1.png"), (io.BytesIO(fresh), "c2.png")],
        "on_duplicate": "ask",
    }, content_type="multipart/form-data")
    j = resp.get_json()
    check(j["need_choice"] and len(j["duplicates"]) == 1, "批次内第二张被标为重复")
    check(j["duplicates"][0]["batch_peer_index"] == 0 and j["duplicates"][0]["existing"] is None,
          "批次内重复指向首个文件、无已有记录")
    check(len(j["pending"]) == 1, "首张进入待上传列表")

    # 确认：首张保留，第二张也保留（两条独立记录）
    resp = client.post("/api/images", data={
        "files": [(io.BytesIO(fresh), "c1.png"), (io.BytesIO(fresh), "c2.png")],
        "on_duplicate": "keep", "on_duplicate_1": "keep",
    }, content_type="multipart/form-data")
    j = resp.get_json()
    check(len(j["saved"]) == 2, "确认后两张都建成独立记录")

    # merge_new：覆盖已有名字
    resp = client.post("/api/images", data={"files": (io.BytesIO(data), "z.png"),
                                            "on_duplicate": "merge_new"},
                       content_type="multipart/form-data")
    j = resp.get_json()
    check(len(j["merged"]) == 1 and j["merged"][0]["status"] == "merged_new",
          "merge_new 返回 merged 状态")


def test_legacy_migration():
    print("== 旧数据迁移：id=内容哈希、无 content_hash 字段 ==")
    import json
    from server.storage import atomic_write_json
    data = _png_bytes(color=(3, 3, 3), w=32, h=32)
    import hashlib
    h = hashlib.sha256(data).hexdigest()
    stored = h + ".png"
    with open(os.path.join(config.IMAGES_DIR, stored), "wb") as f:
        f.write(data)
    legacy = {h: {
        "id": h, "filename": "old.png", "stored_name": stored,
        "hash": h, "ext": ".png", "format": "PNG", "width": 32, "height": 32,
        "size_bytes": len(data), "created_at": "2020-01-01T00:00:00+00:00",
        "tags": [], "note": "", "annotations": [],
    }}
    atomic_write_json(config.IMAGES_JSON, legacy)

    store = ImageStore()
    rec = store.get(h)
    check(rec is not None and rec["content_hash"] == h, "旧记录补齐 content_hash")
    check(store.file_path(h).endswith(stored), "旧记录 id 与文件指针继续有效")
    # 同内容 keep 上传：新 uuid 记录 + 共享文件
    r2, s2 = store.save_upload(data, "old-copy.png", policy="keep")
    check(s2 == "created" and r2["id"] != h and r2["content_hash"] == h,
          "旧记录旁可建立独立新记录")
    check(store.duplicate_counts()[h] == 2, "迁移后重复计数正确")


def main():
    tmp = TempData()
    try:
        test_store_policies()
        test_http()
        test_legacy_migration()
    finally:
        tmp.cleanup()
    print("\n重复上传相关测试全部通过 ✔")


if __name__ == "__main__":
    main()
