"""图像文件存储与元数据管理。

设计要点：

- 「记录身份」与「内容身份」分离：每条上传记录有独立 id（uuid4），可以给同一内容
  保留多个不同名字/标签的记录；内容指纹（SHA-256）放在 content_hash 字段，
  用于查重与结果缓存（hash 字段保留为 content_hash 的别名，供缓存键使用）。
- 物理文件仍按内容去重存放：磁盘文件名 = 内容哈希 + 扩展名，多个记录共享同一份
  原图与缩略图；删除某条记录时只在没有其它记录引用该内容时才真正删文件。
- 遇到内容相同的上传不再静默吞掉新名字：save_upload 接收 policy：
    ask       - 不处理，交由上层提示用户选择（默认）
    keep      - 按新名字生成一条独立记录
    merge     - 复用已有记录，保留旧名字/标签
    merge_new - 复用已有记录，但用新上传的名字/标签覆盖
- 写序保证一致性：先把图像字节原子落盘，再原子更新 images.json；
  中途崩溃只会留下「孤儿文件」，reconcile() 能把它识别出来。
"""
import hashlib
import io
import os
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .storage import JsonStore, atomic_write_bytes, now_iso


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extension_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in config.ALLOWED_EXTENSIONS:
        return ext
    return ".png"


class ImageStore:
    def __init__(self):
        self.meta = JsonStore(config.IMAGES_JSON, {})
        self._migrate_legacy()

    @staticmethod
    def content_hash_of(data: bytes) -> str:
        """内容指纹（SHA-256），供上传预检查重。"""
        return _content_hash(data)

    # ------------------------------------------------------------------ 读
    def list_records(self):
        """按创建时间倒序返回所有图像记录列表。"""
        recs = list(self.meta.read().values())
        recs.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")), reverse=True)
        return recs

    def get(self, image_id):
        return self.meta.read().get(image_id)

    def file_path(self, image_id):
        rec = self.get(image_id)
        if not rec:
            return None
        return os.path.join(config.IMAGES_DIR, rec["stored_name"])

    def thumbnail_path_for(self, content_hash):
        """缩略图按内容命名：同内容的记录共享一张缩略图。"""
        return os.path.join(config.THUMBS_DIR, f"{content_hash}.jpg")

    def thumbnail_path(self, image_id):
        rec = self.get(image_id)
        if not rec:
            return None
        return self.thumbnail_path_for(rec["content_hash"])

    def find_by_content(self, content_hash):
        """返回该内容最早的一条上传记录（同内容多条时以首传为准），没有则 None。"""
        peers = [r for r in self.meta.read().values() if r.get("content_hash") == content_hash]
        if not peers:
            return None
        peers.sort(key=lambda r: (r.get("created_at", ""), r.get("id", "")))
        return peers[0]

    def duplicate_counts(self):
        """返回 {content_hash: 记录数}，只包含出现次数 > 1 的内容。"""
        counts = {}
        for rec in self.meta.read().values():
            h = rec.get("content_hash")
            if h:
                counts[h] = counts.get(h, 0) + 1
        return {h: n for h, n in counts.items() if n > 1}

    # ------------------------------------------------------------------ 写
    def save_upload(self, data: bytes, filename: str, policy: str = "ask",
                    tags=None):
        """保存一次上传。

        返回 (record, status)：
          created      - 新建了一条记录（内容首次出现，或 policy=keep 保留副本）
          duplicate    - 内容已存在且 policy=ask，未做任何改动
          merged       - 内容已存在，复用旧记录（保留旧名字/标签）
          merged_new   - 内容已存在，复用旧记录但覆盖为新名字/标签
        """
        content_hash = _content_hash(data)
        existing = self.find_by_content(content_hash)

        if existing is not None:
            if policy == "ask":
                return existing, "duplicate"
            if policy == "keep":
                return self._create_record(data, filename, content_hash, tags), "created"
            if policy == "merge_new":
                fields = {}
                if filename:
                    fields["filename"] = filename
                if tags is not None:
                    fields["tags"] = list(tags)
                if fields:
                    rec = self.update_meta(existing["id"], fields)
                    return rec, "merged_new"
                return existing, "merged"
            # policy == "merge"：静默复用旧记录的旧行为
            return existing, "merged"

        return self._create_record(data, filename, content_hash, tags), "created"

    def _create_record(self, data: bytes, filename: str, content_hash: str,
                       tags=None):
        """为（可能与其它记录共享物理文件的）一份内容新建一条独立记录。"""
        # 同内容已有物理文件（keep 副本场景）：沿用其扩展名，避免重复落盘
        shared = self.find_by_content(content_hash)
        if shared is not None:
            stored_name = shared["stored_name"]
            width, height, fmt = shared["width"], shared["height"], shared["format"]
        else:
            ext = _extension_for(filename)
            stored_name = content_hash + ext
            dest = os.path.join(config.IMAGES_DIR, stored_name)

            # 1) 先解析尺寸/格式；无法识别则不落盘
            try:
                img = Image.open(io.BytesIO(data))
                width, height = img.size
                fmt = img.format or ext[1:].upper()
            except UnidentifiedImageError:
                raise ValueError("无法识别的图像格式")

            # 2) 原子落图像文件
            atomic_write_bytes(dest, data)

        image_id = uuid.uuid4().hex
        record = {
            "id": image_id,
            "filename": filename or stored_name,
            "stored_name": stored_name,
            "content_hash": content_hash,
            "hash": content_hash,
            "ext": os.path.splitext(stored_name)[1],
            "format": fmt,
            "width": width,
            "height": height,
            "size_bytes": len(data),
            "created_at": now_iso(),
            "tags": list(tags) if tags else [],
            "note": "",
            "annotations": [],
        }

        # 3) 再原子更新元数据
        def _add(doc):
            doc = dict(doc)
            doc[image_id] = record
            return doc

        self.meta.update(_add)

        # 4) 缩略图按内容共享，缺失才生成
        thumb = self.thumbnail_path_for(content_hash)
        if not os.path.exists(thumb):
            self._make_thumbnail(thumb, data)
        return record

    def _make_thumbnail(self, thumb_path, data: bytes):
        """生成缩略图；失败不致命（保留空缩略图路径）。"""
        try:
            os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((config.THUMB_DIM, config.THUMB_DIM), Image.Resampling.LANCZOS)
            tmp = thumb_path + ".tmp"
            img.save(tmp, "JPEG", quality=82)
            os.replace(tmp, thumb_path)
        except Exception:
            try:
                os.unlink(thumb_path + ".tmp")
            except OSError:
                pass

    def update_meta(self, image_id, fields):
        """更新 tags / note / filename 等轻量字段。"""
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec:
                rec = dict(rec)
                for k in ("tags", "note", "filename"):
                    if k in fields:
                        rec[k] = fields[k]
                rec["updated_at"] = now_iso()
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def add_annotation(self, image_id, box, label, color="#ff5252"):
        """给图像加一条人工标注（目标检测标注页）。"""
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec:
                rec = dict(rec)
                rec.setdefault("annotations", []).append({
                    "box": box, "label": label, "color": color, "created_at": now_iso(),
                })
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def delete_annotation(self, image_id, index):
        def _upd(doc):
            doc = dict(doc)
            rec = doc.get(image_id)
            if rec and "annotations" in rec:
                rec = dict(rec)
                anns = list(rec["annotations"])
                if 0 <= index < len(anns):
                    anns.pop(index)
                rec["annotations"] = anns
                doc[image_id] = rec
            return doc

        return self.meta.update(_upd).get(image_id)

    def delete(self, image_id):
        """删除一条记录。

        原图与缩略图按内容共享：仅当没有其它记录引用同一内容时才真正删除文件
        （结果图保留，历史里仍能查看）。
        """
        rec = self.get(image_id)
        if not rec:
            return False

        def _upd(doc):
            doc = dict(doc)
            doc.pop(image_id, None)
            return doc

        doc = self.meta.update(_upd)

        content_hash = rec.get("content_hash")
        still_used = any(
            r.get("content_hash") == content_hash and r.get("id") != image_id
            for r in doc.values()
        )
        if not still_used:
            for p in (os.path.join(config.IMAGES_DIR, rec.get("stored_name", "")),
                      self.thumbnail_path_for(content_hash)):
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass
        return True

    # -------------------------------------------------------------- 迁移
    def _migrate_legacy(self):
        """旧版本记录以内容哈希为 id，且没有独立 content_hash 字段。

        补齐 content_hash/hash 字段即可，id 保持不变（历史/批次里的旧引用继续有效）。
        """
        def _upd(doc):
            doc = dict(doc)
            changed = False
            for rid, rec in doc.items():
                rec = dict(rec)
                if not rec.get("content_hash"):
                    rec["content_hash"] = rec.get("hash") or rid
                    rec["hash"] = rec["content_hash"]
                    doc[rid] = rec
                    changed = True
            return doc if changed else doc

        self.meta.update(_upd)

    # -------------------------------------------------------------- 一致性
    def reconcile(self):
        """校验「JSON 元数据」与「图像文件」的一致性。

        返回 issues 字典：
          orphan_meta  - 元数据存在但文件缺失（悬空引用）
          orphan_files - 文件存在但元数据没有（孤儿文件，可回收）
        """
        records = self.meta.read()
        issues = {"orphan_meta": [], "orphan_files": []}

        missing = set()
        for image_id, rec in records.items():
            p = os.path.join(config.IMAGES_DIR, rec.get("stored_name", ""))
            if not os.path.exists(p):
                issues["orphan_meta"].append({"id": image_id, "filename": rec.get("filename")})
                missing.add(rec.get("stored_name", ""))

        known_names = {rec.get("stored_name") for rec in records.values()}
        for fn in os.listdir(config.IMAGES_DIR):
            if fn.startswith(".tmp-"):
                continue
            if fn not in known_names:
                issues["orphan_files"].append(fn)

        return issues

    def clean_orphan_files(self):
        """删除无元数据引用的孤儿文件，返回删除数量。"""
        records = self.meta.read()
        known = {rec.get("stored_name") for rec in records.values()}
        removed = 0
        for fn in os.listdir(config.IMAGES_DIR):
            if fn.startswith(".tmp-"):
                continue
            if fn not in known:
                try:
                    os.unlink(os.path.join(config.IMAGES_DIR, fn))
                    removed += 1
                except OSError:
                    pass
        return removed
