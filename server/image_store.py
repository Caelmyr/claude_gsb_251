"""图像文件存储与元数据管理。

- 采用「内容寻址」存物理文件：上传即计算 SHA-256，磁盘文件名 = 内容哈希 + 扩展名，
  相同内容的图只占一份磁盘空间。但**每次上传都是一条独立记录**（UUID 主键），
  各自拥有文件名、标签、备注与上传时间——同名同内容不会静默吞掉用户起的新名字。
- 上传重复内容时由调用方决定策略（save_upload 的 on_duplicate）：
    * "ask"      ：不写记录，返回已有记录信息，交由上层向用户确认；
    * "copy"     ：以本次上传的文件名新建一条独立记录（默认）；
    * "reuse"    ：复用最早那条记录（旧的静默去重行为）；
    * "overwrite"：保留最早记录 ID，但把文件名更新为本次上传的名字。
- 写序保证一致性：先把图像字节原子落盘，再原子更新 images.json；
  中途崩溃只会留下「孤儿文件」，reconcile() 能把它识别出来。
- 缩略图按内容哈希共享，独立生成，前端列表/预览不拖全尺寸大图。
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

    # ------------------------------------------------------------------ 读
    def list_records(self):
        """按创建时间倒序返回所有图像记录列表。"""
        recs = list(self.meta.read().values())
        recs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return recs

    def get(self, image_id):
        return self.meta.read().get(image_id)

    def _records_by_hash(self, content_hash):
        """返回同一内容哈希的全部记录，按上传时间正序（最早在前）。"""
        recs = [r for r in self.meta.read().values() if r.get("hash") == content_hash]
        recs.sort(key=lambda r: r.get("created_at", ""))
        return recs

    def find_duplicates(self, content_hash):
        """返回与给定内容哈希相同的已有记录（最早在前）。"""
        return self._records_by_hash(content_hash)

    def file_path(self, image_id):
        rec = self.get(image_id)
        if not rec:
            return None
        return os.path.join(config.IMAGES_DIR, rec["stored_name"])

    def thumbnail_path(self, image_id):
        """缩略图按内容哈希共享：同内容的所有记录用同一张缩略图。"""
        rec = self.get(image_id)
        if not rec:
            return None
        return os.path.join(config.THUMBS_DIR, f"{rec['hash']}.jpg")

    # ------------------------------------------------------------------ 写
    def save_upload(self, data: bytes, filename: str, on_duplicate: str = "copy"):
        """保存一次上传。

        返回 (record, status)：
          status == "created"   新内容，首次落盘；
          status == "copy"      重复内容，按 on_duplicate="copy" 新建了独立记录；
          status == "reused"    重复内容，复用了已有记录（on_duplicate="reuse"）；
          status == "overwritten" 重复内容，已有记录被改名为本次文件名；
          status == "duplicate" on_duplicate="ask"，未写入，record 为最早的重复记录。
        """
        content_hash = _content_hash(data)
        existing = self._records_by_hash(content_hash)

        if existing and on_duplicate == "ask":
            return existing[0], "duplicate"
        if existing and on_duplicate == "reuse":
            return existing[0], "reused"
        if existing and on_duplicate == "overwrite":
            rec = self.update_meta(existing[0]["id"], {"filename": filename or existing[0]["stored_name"]})
            return rec, "overwritten"

        ext = _extension_for(filename)
        stored_name = content_hash + ext
        dest = os.path.join(config.IMAGES_DIR, stored_name)

        # 1) 先落图像文件（原子）；同内容已存在则复用，不重复写
        if not existing:
            atomic_write_bytes(dest, data)

        # 2) 读取尺寸/格式（任意一条同内容记录均可，优先复用已解析结果）
        if existing:
            ref = existing[0]
            width, height, fmt = ref["width"], ref["height"], ref["format"]
        else:
            try:
                img = Image.open(io.BytesIO(data))
                width, height = img.size
                fmt = (img.format or ext[1:].upper())
            except UnidentifiedImageError:
                # 落盘失败清理，向上抛
                try:
                    os.unlink(dest)
                except OSError:
                    pass
                raise ValueError("无法识别的图像格式")

        image_id = uuid.uuid4().hex
        record = {
            "id": image_id,
            "filename": filename or stored_name,
            "stored_name": stored_name,
            "hash": content_hash,
            "ext": ext,
            "format": fmt,
            "width": width,
            "height": height,
            "size_bytes": len(data),
            "created_at": now_iso(),
            "tags": [],
            "note": "",
            "annotations": [],
        }

        # 3) 再原子更新元数据
        def _add(doc):
            doc = dict(doc)
            doc[image_id] = record
            return doc

        self.meta.update(_add)
        if not existing:
            self._make_thumbnail(content_hash, data)
        return record, ("created" if not existing else "copy")

    def _make_thumbnail(self, content_hash, data: bytes):
        """生成缩略图（按内容哈希命名，天然共享）；失败不致命。"""
        thumb = os.path.join(config.THUMBS_DIR, f"{content_hash}.jpg")
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((config.THUMB_DIM, config.THUMB_DIM), Image.Resampling.LANCZOS)
            tmp = thumb + ".tmp"
            img.save(tmp, "JPEG", quality=82)
            os.replace(tmp, thumb)
        except Exception:
            try:
                os.unlink(thumb + ".tmp")
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
        """删除一条图像记录。

        物理文件与缩略图按内容引用计数：同内容仍有其他记录时保留，
        最后一条记录删除时才真正回收磁盘文件（结果图保留，历史里仍能查看）。
        """
        rec = self.get(image_id)
        if not rec:
            return False

        def _upd(doc):
            doc = dict(doc)
            doc.pop(image_id, None)
            return doc

        self.meta.update(_upd)

        # 重新统计同一内容哈希的剩余引用
        if not self._records_by_hash(rec["hash"]):
            for p in (os.path.join(config.IMAGES_DIR, rec["stored_name"]),
                      os.path.join(config.THUMBS_DIR, f"{rec['hash']}.jpg")):
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass
        return True

    # -------------------------------------------------------------- 一致性
    def reconcile(self):
        """校验「JSON 元数据」与「图像文件」的一致性。

        返回 issues 字典：
          orphan_meta  - 元数据存在但文件缺失（悬空引用）
          orphan_files - 文件存在但元数据没有（孤儿文件，可回收）
        """
        records = self.meta.read()
        issues = {"orphan_meta": [], "orphan_files": []}

        for image_id, rec in records.items():
            p = os.path.join(config.IMAGES_DIR, rec.get("stored_name", ""))
            if not os.path.exists(p):
                issues["orphan_meta"].append({"id": image_id, "filename": rec.get("filename")})

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
