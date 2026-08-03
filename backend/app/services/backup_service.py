"""
数据备份与恢复服务

参考 qmediasync 的 backup 模块设计。

功能：
1. 备份 data/ 目录下所有 .json 文件为 zip 压缩包
2. 从 zip 压缩包恢复数据（恢复前自动备份当前数据）
3. 备份记录持久化存储在 settings.json 的 "backup_records" 键
4. 运行状态实时跟踪（线程安全）
5. 旧备份自动清理

备份文件存放：data/backups/backup_{type}_{timestamp}.zip
"""
import os
import time
import zipfile
import tempfile
import threading
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR, CONFIG_DIR
from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.backup_service")

# 备份文件存放目录
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

# 备份记录在 settings.json 中的键名
_BACKUP_RECORDS_KEY = "backup_records"

# 线程锁：保证备份/恢复操作串行执行
_lock = threading.Lock()


class BackupService:
    """
    数据备份与恢复服务

    提供 JSON 数据文件的备份、恢复、查询、删除、清理功能。
    所有操作线程安全，同一时间仅允许一个备份或恢复任务运行。
    """

    def __init__(self) -> None:
        # 运行状态（内存中维护，非持久化）
        self._running_status: dict = {
            "is_running": False,
            "type": "",          # backup / restore
            "desc": "",
            "total": 0,
            "count": 0,
            "error_msg": "",
            "start_time": 0,
            "elapsed": 0.0,
        }
        self._status_lock = threading.Lock()

    # ===== 内部辅助方法 =====

    @staticmethod
    def _read_records() -> dict:
        """读取备份记录，返回 {"records": [...], "next_id": int}"""
        data = read_setting(_BACKUP_RECORDS_KEY)
        if not isinstance(data, dict):
            return {"records": [], "next_id": 1}
        if "records" not in data:
            data["records"] = []
        if "next_id" not in data or not isinstance(data["next_id"], int):
            data["next_id"] = 1
        return data

    @staticmethod
    def _write_records(data: dict) -> bool:
        """写入备份记录到 settings.json"""
        return save_setting(_BACKUP_RECORDS_KEY, data)

    def _update_status(self, **kwargs) -> None:
        """更新运行状态（线程安全）"""
        with self._status_lock:
            self._running_status.update(kwargs)
            if self._running_status.get("start_time"):
                self._running_status["elapsed"] = round(
                    time.time() - self._running_status["start_time"], 2
                )

    def _set_running(self, task_type: str, desc: str, total: int = 0) -> None:
        """标记任务开始运行"""
        with self._status_lock:
            self._running_status = {
                "is_running": True,
                "type": task_type,
                "desc": desc,
                "total": total,
                "count": 0,
                "error_msg": "",
                "start_time": time.time(),
                "elapsed": 0.0,
            }

    def _set_finished(self, error_msg: str = "") -> None:
        """标记任务结束"""
        with self._status_lock:
            self._running_status["is_running"] = False
            self._running_status["error_msg"] = error_msg
            if self._running_status.get("start_time"):
                self._running_status["elapsed"] = round(
                    time.time() - self._running_status["start_time"], 2
                )

    def _add_record(self, record: dict) -> int:
        """新增一条备份记录，返回分配的 ID"""
        data = self._read_records()
        record_id = data["next_id"]
        record["id"] = record_id
        data["records"].append(record)
        data["next_id"] = record_id + 1
        self._write_records(data)
        return record_id

    def _update_record(self, record_id: int, **kwargs) -> None:
        """更新指定备份记录的字段"""
        data = self._read_records()
        for rec in data["records"]:
            if rec.get("id") == record_id:
                rec.update(kwargs)
                break
        self._write_records(data)

    @staticmethod
    def _scan_json_files() -> list[tuple[str, Path]]:
        """
        扫描 data/ 和 config/ 目录下所有 .json 文件（不含 backups 子目录）。
        返回 [(存储相对路径, 文件绝对路径), ...]
        存储相对路径格式: data/xxx.json 或 config/xxx.json
        """
        json_files: list[tuple[str, Path]] = []
        # 扫描 data/ 目录（顶层）
        for item in DATA_DIR.iterdir():
            if item.is_file() and item.suffix == ".json":
                json_files.append((f"data/{item.name}", item))
        # 扫描 config/ 目录（顶层）
        if CONFIG_DIR.exists():
            for item in CONFIG_DIR.iterdir():
                if item.is_file() and item.suffix == ".json":
                    json_files.append((f"config/{item.name}", item))
        return json_files

    @staticmethod
    def _atomic_write_zip(zip_path: Path, json_files: list[tuple[str, bytes]], progress_cb=None) -> int:
        """
        原子写入 zip 文件：先写临时文件再重命名。
        返回写入的文件数量。

        Args:
            zip_path: 最终 zip 文件路径
            json_files: [(文件名, 文件内容字节), ...]
            progress_cb: 进度回调 callback(count, total, desc)
        """
        total = len(json_files)
        # 在同目录下创建临时文件
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", dir=str(zip_path.parent), prefix=".backup_"
        )
        os.close(tmp_fd)
        tmp_file = Path(tmp_path)

        try:
            with zipfile.ZipFile(tmp_file, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, (name, content) in enumerate(json_files):
                    zf.writestr(name, content)
                    if progress_cb:
                        progress_cb(i + 1, total, f"正在备份 {name}")
            # 重命名为最终文件
            tmp_file.replace(zip_path)
            return total
        except Exception:
            # 清理临时文件
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            raise

    # ===== 公开 API =====

    def backup(self, backup_type: str = "manual", reason: str = "") -> dict:
        """
        执行数据备份。

        流程：
        1. 创建 backup_record（状态 running）
        2. 遍历 data/ 目录下所有 .json 文件，读取内容
        3. 打包为 zip 文件（data/backups/backup_{type}_{timestamp}.zip）
        4. 更新 backup_record 状态为 completed
        5. 记录文件大小、耗时、文件数量

        Args:
            backup_type: 备份类型（manual / auto / pre_restore 等）
            reason: 备份原因描述

        Returns:
            备份结果 dict，包含 success, backup_id, file_path, file_size 等
        """
        with _lock:
            # 检查是否已有任务运行
            if self._running_status.get("is_running"):
                return {
                    "success": False,
                    "error_msg": f"已有{self._running_status.get('type', '')}任务正在运行",
                }

            start_time = time.time()
            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(start_time))
            zip_filename = f"backup_{backup_type}_{timestamp}.zip"
            zip_path = BACKUP_DIR / zip_filename

            # 创建备份记录
            record_id = self._add_record({
                "type": backup_type,
                "reason": reason or ("手动备份" if backup_type == "manual" else ""),
                "status": "running",
                "file_path": str(zip_path.relative_to(DATA_DIR)) if zip_path.exists() else zip_filename,
                "file_size": 0,
                "file_count": 0,
                "duration": 0.0,
                "created_at": int(start_time),
                "error_msg": "",
            })

            self._set_running("backup", "正在扫描数据文件...")

            try:
                # 扫描 JSON 文件
                json_files_paths = self._scan_json_files()
                if not json_files_paths:
                    self._update_status(desc="没有找到需要备份的 JSON 文件")
                    logger.warning("备份：data/ 目录下没有 JSON 文件")

                self._update_status(total=len(json_files_paths))

                # 读取文件内容
                file_contents: list[tuple[str, bytes]] = []
                for i, (rel_name, fpath) in enumerate(json_files_paths):
                    try:
                        content = fpath.read_bytes()
                        file_contents.append((rel_name, content))
                        self._update_status(
                            count=i + 1,
                            desc=f"正在读取 {fpath.name}",
                        )
                    except Exception as e:
                        logger.warning(f"读取文件 {fpath.name} 失败: {e}")

                self._update_status(desc="正在打包压缩...")

                # 进度回调
                def _progress_cb(count: int, total: int, desc: str) -> None:
                    self._update_status(count=count, total=total, desc=desc)

                # 原子写入 zip
                file_count = self._atomic_write_zip(zip_path, file_contents, _progress_cb)

                duration = round(time.time() - start_time, 2)
                file_size = zip_path.stat().st_size if zip_path.exists() else 0

                # 更新记录为完成
                self._update_record(
                    record_id,
                    status="completed",
                    file_path=str(zip_path.relative_to(DATA_DIR)) if zip_path.exists() else zip_filename,
                    file_size=file_size,
                    file_count=file_count,
                    duration=duration,
                    error_msg="",
                )

                self._set_finished()
                logger.info(
                    f"备份完成: id={record_id}, file={zip_filename}, "
                    f"size={file_size}, count={file_count}, duration={duration}s"
                )

                return {
                    "success": True,
                    "backup_id": record_id,
                    "file_path": str(zip_path.relative_to(DATA_DIR)) if zip_path.exists() else zip_filename,
                    "file_size": file_size,
                    "file_count": file_count,
                    "duration": duration,
                }

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.warning(f"备份失败: {error_msg}")
                duration = round(time.time() - start_time, 2)
                self._update_record(
                    record_id,
                    status="failed",
                    duration=duration,
                    error_msg=error_msg,
                )
                self._set_finished(error_msg=error_msg)
                return {
                    "success": False,
                    "error_msg": error_msg,
                    "backup_id": record_id,
                }

    def restore(self, zip_path: str) -> dict:
        """
        从 zip 压缩包恢复数据。

        流程：
        1. 恢复前先备份当前数据（安全措施，类型为 pre_restore）
        2. 解压 zip 文件
        3. 遍历解压后的 JSON 文件，覆盖 data/ 目录对应文件
        4. 原子写入每个文件（先写临时文件再重命名）

        Args:
            zip_path: zip 文件路径（绝对路径或相对于 data/ 的路径）

        Returns:
            恢复结果 dict，包含 success, restored_count, pre_backup_id 等
        """
        with _lock:
            if self._running_status.get("is_running"):
                return {
                    "success": False,
                    "error_msg": f"已有{self._running_status.get('type', '')}任务正在运行",
                }

            # 解析 zip 路径
            zip_file = Path(zip_path)
            if not zip_file.is_absolute():
                zip_file = DATA_DIR / zip_path

            if not zip_file.exists():
                return {"success": False, "error_msg": f"备份文件不存在: {zip_file}"}

            if not zipfile.is_zipfile(zip_file):
                return {"success": False, "error_msg": "不是有效的 zip 文件"}

            self._set_running("restore", "正在创建恢复前安全备份...")

            pre_backup_id: Optional[int] = None
            try:
                # 恢复前先备份当前数据
                current_files = self._scan_json_files()
                if current_files:
                    pre_timestamp = time.strftime("%Y%m%d_%H%M%S")
                    pre_zip_name = f"backup_pre_restore_{pre_timestamp}.zip"
                    pre_zip_path = BACKUP_DIR / pre_zip_name

                    pre_record_id = self._add_record({
                        "type": "pre_restore",
                        "reason": f"恢复 {zip_file.name} 前的自动备份",
                        "status": "running",
                        "file_path": pre_zip_name,
                        "file_size": 0,
                        "file_count": 0,
                        "duration": 0.0,
                        "created_at": int(time.time()),
                        "error_msg": "",
                    })

                    pre_start = time.time()
                    try:
                        pre_contents = []
                        for rel_name, fpath in current_files:
                            try:
                                pre_contents.append((rel_name, fpath.read_bytes()))
                            except Exception as e:
                                logger.warning(f"安全备份读取 {fpath.name} 失败: {e}")

                        pre_count = self._atomic_write_zip(pre_zip_path, pre_contents)
                        pre_size = pre_zip_path.stat().st_size if pre_zip_path.exists() else 0
                        pre_duration = round(time.time() - pre_start, 2)

                        self._update_record(
                            pre_record_id,
                            status="completed",
                            file_size=pre_size,
                            file_count=pre_count,
                            duration=pre_duration,
                        )
                        pre_backup_id = pre_record_id
                        logger.info(f"恢复前安全备份完成: id={pre_record_id}")
                    except Exception as e:
                        logger.warning(f"恢复前安全备份失败: {e}")
                        self._update_record(
                            pre_record_id,
                            status="failed",
                            error_msg=str(e),
                        )

                # 解压并恢复
                self._update_status(desc="正在解压备份文件...")

                with zipfile.ZipFile(zip_file, "r") as zf:
                    json_entries = [
                        name for name in zf.namelist()
                        if name.endswith(".json") and not name.startswith("__MACOSX")
                    ]
                    self._update_status(total=len(json_entries))

                    restored_count = 0
                    for i, name in enumerate(json_entries):
                        try:
                            content = zf.read(name)
                            # 根据备份时的相对路径前缀决定恢复到哪个目录
                            name_stripped = name.replace("\\", "/")
                            if name_stripped.startswith("config/"):
                                target_dir = CONFIG_DIR
                                target_path = target_dir / Path(name).name
                            else:
                                # 兼容旧备份（无前缀，默认 data/）和带 data/ 前缀的
                                target_dir = DATA_DIR
                                target_path = target_dir / Path(name).name

                            target_dir.mkdir(parents=True, exist_ok=True)

                            # 原子写入：先写临时文件再重命名
                            tmp_fd, tmp_path = tempfile.mkstemp(
                                suffix=".tmp",
                                dir=str(DATA_DIR),
                                prefix=f".restore_{Path(name).stem}_",
                            )
                            os.close(tmp_fd)
                            tmp_file = Path(tmp_path)
                            try:
                                tmp_file.write_bytes(content)
                                tmp_file.replace(target_path)
                                restored_count += 1
                            except Exception:
                                if tmp_file.exists():
                                    try:
                                        tmp_file.unlink()
                                    except OSError:
                                        pass
                                raise

                            self._update_status(
                                count=i + 1,
                                desc=f"正在恢复 {Path(name).name}",
                            )
                        except Exception as e:
                            logger.warning(f"恢复文件 {name} 失败: {e}")

                duration = round(
                    time.time() - self._running_status.get("start_time", time.time()), 2
                )
                self._set_finished()

                logger.info(f"恢复完成: restored={restored_count}/{len(json_entries)}")
                return {
                    "success": True,
                    "restored_count": restored_count,
                    "total_count": len(json_entries),
                    "pre_backup_id": pre_backup_id,
                    "duration": duration,
                }

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.warning(f"恢复失败: {error_msg}")
                self._set_finished(error_msg=error_msg)
                return {
                    "success": False,
                    "error_msg": error_msg,
                    "pre_backup_id": pre_backup_id,
                }

    def get_running_status(self) -> dict:
        """
        获取备份/恢复进度。

        Returns:
            运行状态 dict：
            {
                "is_running": False,
                "type": "backup",      # backup / restore / ""
                "desc": "正在备份 settings.json",
                "total": 5,
                "count": 2,
                "error_msg": "",
                "start_time": 1234567890,
                "elapsed": 1.5
            }
        """
        with self._status_lock:
            status = dict(self._running_status)
            if status.get("is_running") and status.get("start_time"):
                status["elapsed"] = round(time.time() - status["start_time"], 2)
            return status

    def list_backups(self) -> list[dict]:
        """
        列出所有备份记录（按创建时间倒序）。

        Returns:
            备份记录列表
        """
        data = self._read_records()
        records = data.get("records", [])
        # 按创建时间倒序
        return sorted(records, key=lambda r: r.get("created_at", 0), reverse=True)

    def delete_backup(self, backup_id: int) -> dict:
        """
        删除指定备份（删除记录 + 删除 zip 文件）。

        Args:
            backup_id: 备份记录 ID

        Returns:
            操作结果 dict
        """
        data = self._read_records()
        target_record: Optional[dict] = None
        new_records = []
        for rec in data["records"]:
            if rec.get("id") == backup_id:
                target_record = rec
            else:
                new_records.append(rec)

        if target_record is None:
            return {"success": False, "error_msg": f"备份记录不存在: id={backup_id}"}

        # 删除 zip 文件
        file_path = target_record.get("file_path", "")
        if file_path:
            zip_file = Path(file_path)
            if not zip_file.is_absolute():
                zip_file = DATA_DIR / file_path
            if zip_file.exists():
                try:
                    zip_file.unlink()
                    logger.info(f"已删除备份文件: {zip_file}")
                except OSError as e:
                    logger.warning(f"删除备份文件失败: {e}")

        # 更新记录
        data["records"] = new_records
        self._write_records(data)

        logger.info(f"已删除备份记录: id={backup_id}")
        return {"success": True, "deleted_id": backup_id}

    def cleanup_old_backups(self, max_count: int = 10) -> dict:
        """
        清理旧备份，仅保留最新的 max_count 条记录。

        自动备份（type=auto / pre_restore）优先被清理，
        手动备份（type=manual）尽量保留。

        Args:
            max_count: 最大保留备份数量

        Returns:
            清理结果 dict，包含 deleted_count 和 deleted_ids
        """
        data = self._read_records()
        records = data.get("records", [])

        if len(records) <= max_count:
            return {
                "success": True,
                "deleted_count": 0,
                "deleted_ids": [],
                "remaining_count": len(records),
                "message": f"当前备份数 {len(records)} 未超过上限 {max_count}，无需清理",
            }

        # 按创建时间倒序排列
        sorted_records = sorted(
            records, key=lambda r: r.get("created_at", 0), reverse=True
        )

        # 保留最新 max_count 条
        keep_records = sorted_records[:max_count]
        delete_records = sorted_records[max_count:]

        deleted_ids: list[int] = []
        for rec in delete_records:
            rec_id = rec.get("id")
            if rec_id is None:
                continue
            # 删除 zip 文件
            file_path = rec.get("file_path", "")
            if file_path:
                zip_file = Path(file_path)
                if not zip_file.is_absolute():
                    zip_file = DATA_DIR / file_path
                if zip_file.exists():
                    try:
                        zip_file.unlink()
                    except OSError as e:
                        logger.warning(f"清理备份文件失败 {zip_file}: {e}")
            deleted_ids.append(rec_id)

        # 更新记录
        data["records"] = keep_records
        self._write_records(data)

        logger.info(f"清理旧备份完成: 删除 {len(deleted_ids)} 条，保留 {len(keep_records)} 条")
        return {
            "success": True,
            "deleted_count": len(deleted_ids),
            "deleted_ids": deleted_ids,
            "remaining_count": len(keep_records),
        }


# 全局单例
backup_service = BackupService()
