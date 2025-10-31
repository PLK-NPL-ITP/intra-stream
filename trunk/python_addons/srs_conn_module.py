from database import Database
from srs_logger import get_logger
import requests
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
import uuid
import subprocess
import os
import shutil
from pathlib import Path
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================================
# System Statistics Management Module
# ============================================================================

@dataclass
class PollingTask:
    """轮询任务配置"""
    task_id: str
    name: str
    handler: Callable[[], bool]
    interval: int
    enabled: bool = True
    retry_count: int = 0
    max_retries: int = 5
    last_execution: Optional[float] = None
    thread: Optional[threading.Thread] = None
    is_running: bool = False

class TaskManager:
    """通用任务管理器"""
    
    def __init__(self):
        self.tasks: Dict[str, PollingTask] = {}
        self.logger = get_logger()
        self._shutdown_event = threading.Event()
    
    def register_task(self, name: str, handler: Callable[[], bool], interval: int = 3, task_id: str = None) -> str:
        """注册新任务"""
        if task_id is None:
            task_id = str(uuid.uuid4())
        
        task = PollingTask(
            task_id=task_id,
            name=name,
            handler=handler,
            interval=interval
        )
        
        self.tasks[task_id] = task
        self.logger.info(f"Registered task '{name}' with {interval}s interval")
        return task_id
    
    def unregister_task(self, task_id: str) -> bool:
        """注销任务"""
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            self.logger.info(f"Unregistered task {task_id}")
            return True
        return False
    
    def start_task(self, task_id: str) -> bool:
        """启动指定任务"""
        if task_id not in self.tasks:
            self.logger.error(f"Task {task_id} not found")
            return False
        
        task = self.tasks[task_id]
        if task.is_running:
            self.logger.warn(f"Task '{task.name}' is already running")
            return False
        
        task.is_running = True
        task.thread = threading.Thread(target=self._task_loop, args=(task,))
        task.thread.daemon = True
        task.thread.start()
        self.logger.info(f"Started task '{task.name}'")
        return True
    
    def stop_task(self, task_id: str) -> bool:
        """停止指定任务"""
        if task_id not in self.tasks:
            return False
        
        task = self.tasks[task_id]
        if not task.is_running:
            return False
        
        task.is_running = False
        if task.thread:
            task.thread.join(timeout=5)
        self.logger.info(f"Stopped task '{task.name}'")
        return True
    
    def start_all_tasks(self):
        """启动所有任务"""
        for task_id in self.tasks:
            self.start_task(task_id)
    
    def stop_all_tasks(self):
        """停止所有任务"""
        self._shutdown_event.set()
        for task_id in list(self.tasks.keys()):
            self.stop_task(task_id)
        self._shutdown_event.clear()
    
    def get_task_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有任务状态"""
        status = {}
        for task_id, task in self.tasks.items():
            status[task_id] = {
                'name': task.name,
                'interval': task.interval,
                'enabled': task.enabled,
                'is_running': task.is_running,
                'retry_count': task.retry_count,
                'last_execution': task.last_execution
            }
        return status
    
    def _task_loop(self, task: PollingTask):
        """任务执行循环"""
        while task.is_running and not self._shutdown_event.is_set():
            try:
                if task.enabled:
                    start_time = time.time()
                    success = task.handler()
                    task.last_execution = start_time
                    
                    if success:
                        task.retry_count = 0  # 重置重试计数
                    else:
                        task.retry_count += 1
                        if task.retry_count >= task.max_retries:
                            self.logger.error(f"Task '{task.name}' failed {task.max_retries} times, disabling")
                            task.enabled = False
                
            except Exception as e:
                self.logger.exception(f"Error in task '{task.name}': {e}")
                task.retry_count += 1
                if task.retry_count >= task.max_retries:
                    self.logger.error(f"Task '{task.name}' failed {task.max_retries} times, disabling")
                    task.enabled = False
            
            # 等待指定间隔，允许提前停止
            for _ in range(task.interval):
                if not task.is_running or self._shutdown_event.is_set():
                    break
                time.sleep(1)

class SRSConnectionManager:
    """SRS连接管理器"""

    def __init__(self, db: Database, ffmpeg_binary: Optional[str] = None):
        self.db = db
        self.logger = get_logger()
        self.ffmpeg_binary = self._resolve_ffmpeg_binary(ffmpeg_binary)
        self.api_url = "http://python_stats:wMePq3ahpoLRzgsVg7BY9eE82uuJHT0YukD2ZE1JfMY2RjP4e6QnUaKg3V9x5s9M@localhost:1985/api/v1/summaries"
        self.streams_api_url = "http://python_stats:wMePq3ahpoLRzgsVg7BY9eE82uuJHT0YukD2ZE1JfMY2RjP4e6QnUaKg3V9x5s9M@localhost:1985/api/v1/streams/"
        
        # 任务管理器
        self.task_manager = TaskManager()
        
        # 统计数据相关状态
        self.previous_data: Optional[Dict[str, Any]] = None
        self.previous_timestamp: Optional[float] = None
        
        # Stream 状态跟踪
        self.last_seen_streams: Dict[str, datetime] = {}  # stream_code -> last_seen_timestamp
        self.stream_timeout_seconds = 60   # 60秒超时
        self.stream_start_segments: Dict[str, set] = {}  # 记录直播开始前的segment文件名
        
        # 注册默认任务
        self._register_tasks()
        self.logger.info(f"Using ffmpeg binary: {self.ffmpeg_binary}")

    # ============================================================================
    # Task Management and Registration
    # ============================================================================
    
    def _register_tasks(self):
        """注册默认任务"""
        # 系统统计任务 - 每3秒执行一次
        self.task_manager.register_task(
            name="System Statistics",
            handler=self._fetch_system_stats,
            interval=3,
            task_id="system_stats"
        )

        # 系统统计清除任务 - 每300秒执行一次
        self.task_manager.register_task(
            name="System Statistics Cleanup",
            handler=lambda: self.db.delete_expired_system_stats(), 
            interval=300,
            task_id="system_stats_cleanup"
        )

        # Streams数据任务 - 每5秒执行一次
        self.task_manager.register_task(
            name="Streams Data",
            handler=self._fetch_streams_data,
            interval=5,
            task_id="streams_data"
        )
        
        # Stream快照任务 - 每30秒执行一次
        self.task_manager.register_task(
            name="Stream Snapshots",
            handler=self._capture_all_stream_snapshots,
            interval=30,
            task_id="stream_snapshots"
        )
    
    # def register_custom_task(self, name: str, handler: Callable[[], bool], interval: int = 3) -> str:
    #     """注册自定义任务"""
    #     return self.task_manager.register_task(name, handler, interval)
    # 
    # def unregister_task(self, task_id: str) -> bool:
    #     """注销任务"""
    #     return self.task_manager.unregister_task(task_id)
    
    def get_task_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有任务状态"""
        return self.task_manager.get_task_status()

    # ============================================================================
    # Long Polling SubModule for Tracking
    # ============================================================================

    def start_polling(self, task_ids: list = None):
        """启动轮询
        
        Args:
            task_ids: 要启动的任务ID列表，None表示启动所有任务
        """
        if task_ids is None:
            self.task_manager.start_all_tasks()
            self.logger.info("Started all polling tasks")
        else:
            for task_id in task_ids:
                self.task_manager.start_task(task_id)

    def stop_polling(self, task_ids: list = None):
        """停止轮询
        
        Args:
            task_ids: 要停止的任务ID列表，None表示停止所有任务
        """
        if task_ids is None:
            self.task_manager.stop_all_tasks()
            self.logger.info("Stopped all polling tasks")
        else:
            for task_id in task_ids:
                self.task_manager.stop_task(task_id)

    # ============================================================================
    # System Statistics Tracking and Processing
    # ============================================================================

    def _fetch_system_stats(self):
        """获取系统统计数据并插入数据库"""
        try:
            # 发送HTTP请求获取SRS统计数据
            response = requests.get(self.api_url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # 检查返回状态
            if data.get('code') != 0:
                self.logger.error(f"SRS API returned error code: {data.get('code')}")
                return False
            
            # 提取有用的信息
            stats_data = self._extract_stats_data(data)
            if stats_data:
                # 插入数据库
                success = self.db.insert_system_stats(**stats_data)
                if success:
                    self.logger.debug("Successfully inserted system stats")
                    return True
                else:
                    self.logger.error("Failed to insert system stats to database")
                    return False
            else:
                self.logger.error("Failed to extract stats data")
                return False
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"HTTP request failed: {e}")
            return False
        except Exception as e:
            self.logger.exception(f"Unexpected error in fetch_system_stats: {e}")
            return False

    def _extract_stats_data(self, api_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """从API返回数据中提取有用的统计信息"""
        try:
            data_section = api_data.get('data', {})
            self_section = data_section.get('self', {})
            system_section = data_section.get('system', {})
            
            current_timestamp = data_section.get('now_ms', 0) / 1000.0  # 转换为秒
            
            # SRS相关数据
            srs_uptime = self_section.get('srs_uptime', 0.0)
            srs_cpu_percent = self_section.get('cpu_percent', 0.0)
            srs_memory_percent = self_section.get('mem_percent', 0.0)
            
            # 网络流量数据（需要计算KBps）
            srs_recv_bytes = system_section.get('srs_recv_bytes', 0)
            srs_send_bytes = system_section.get('srs_send_bytes', 0)
            srs_sample_time = system_section.get('srs_sample_time', 0) / 1000.0  # 转换为秒
            
            # 计算KBps（需要与上一次的数据比较）
            srs_recv_KBps = 0.0
            srs_send_KBps = 0.0
            
            if (self.previous_data and 
                self.previous_timestamp and 
                current_timestamp > self.previous_timestamp):
                
                time_diff = current_timestamp - self.previous_timestamp
                prev_recv = self.previous_data.get('srs_recv_bytes', 0)
                prev_send = self.previous_data.get('srs_send_bytes', 0)
                
                if time_diff > 0:
                    # 计算字节差值并转换为KBps
                    srs_recv_KBps = max(0, (srs_recv_bytes - prev_recv) / time_diff / 1024)
                    srs_send_KBps = max(0, (srs_send_bytes - prev_send) / time_diff / 1024)
            
            # print(srs_recv_KBps, srs_send_KBps) # DEBUG

            # 磁盘IO数据
            disk_read_KBps = system_section.get('disk_read_KBps', 0.0)
            disk_write_KBps = system_section.get('disk_write_KBps', 0.0)
            
            # 操作系统相关数据
            os_uptime = system_section.get('uptime', 0.0)
            os_cpu_percent = system_section.get('cpu_percent', 0.0)
            os_memory_percent = system_section.get('mem_ram_percent', 0.0)
            
            # 更新上一次的数据
            self.previous_data = {
                'srs_recv_bytes': srs_recv_bytes,
                'srs_send_bytes': srs_send_bytes,
                'srs_sample_time': srs_sample_time
            }
            self.previous_timestamp = current_timestamp
            
            return {
                'srs_uptime': srs_uptime,
                'srs_cpu_percent': srs_cpu_percent,
                'srs_memory_percent': srs_memory_percent,
                'srs_recv_KBps': srs_recv_KBps,
                'srs_send_KBps': srs_send_KBps,
                'disk_read_KBps': disk_read_KBps,
                'disk_write_KBps': disk_write_KBps,
                'os_uptime': os_uptime,
                'os_cpu_percent': os_cpu_percent,
                'os_memory_percent': os_memory_percent
            }
            
        except Exception as e:
            self.logger.exception(f"Failed to extract stats data: {e}")
            return None

    def _process_stats_data(self, raw_stats: list, time_delta: int, time_interval: int) -> list:
        """
        处理原始统计数据，按指定时间间隔进行插值和过滤
        
        Args:
            raw_stats: 原始统计数据列表
            time_delta: 时间范围（秒）
            time_interval: 时间间隔（秒）
        
        Returns:
            处理后的统计数据列表
        """
        try:
            if not raw_stats:
                return []
            
            # 参数验证
            if time_interval <= 0:
                self.logger.warn("Invalid time_interval, using 1 second")
                time_interval = 1
            if time_delta <= 0:
                self.logger.warn("Invalid time_delta, using 60 seconds")
                time_delta = 60
            
            # 转换时间戳并排序
            for stat in raw_stats:
                if isinstance(stat['timestamp'], str):
                    stat['timestamp'] = datetime.fromisoformat(stat['timestamp'])
                elif not isinstance(stat['timestamp'], datetime):
                    # 如果是其他格式，尝试解析
                    stat['timestamp'] = datetime.fromisoformat(str(stat['timestamp']))
            
            # 按时间戳排序
            raw_stats.sort(key=lambda x: x['timestamp'])
            
            # 确定时间范围
            now = datetime.now().replace(microsecond=0)  # 取整到秒
            start_time = now - timedelta(seconds=time_delta)
            
            # 过滤掉超出时间范围的数据
            filtered_stats = [stat for stat in raw_stats if stat['timestamp'] >= start_time]
            
            if not filtered_stats:
                self.logger.warn("No data points in specified time range")
                return []
            
            # 生成目标时间点列表（从最近一次记录向前）
            target_times = []
            current_time = now
            while current_time >= start_time:
                target_times.append(current_time)
                current_time -= timedelta(seconds=time_interval)
            
            # 反转列表，使其按时间顺序排列
            target_times.reverse()
            
            # 对每个目标时间点进行插值
            processed_stats = []
            numeric_fields = [
                'srs_uptime', 'srs_cpu_percent', 'srs_memory_percent',
                'srs_recv_KBps', 'srs_send_KBps', 'disk_read_KBps', 
                'disk_write_KBps', 'os_uptime', 'os_cpu_percent', 'os_memory_percent'
            ]
            
            for target_time in target_times:
                interpolated_data = self._interpolate_data_point(filtered_stats, target_time, numeric_fields)
                if interpolated_data:
                    processed_stats.append(interpolated_data)
            
            self.logger.debug(f"Processed {len(raw_stats)} raw points into {len(processed_stats)} interpolated points")
            return processed_stats
            
        except Exception as e:
            self.logger.exception(f"Error processing stats data: {e}")
            return raw_stats  # 如果处理失败，返回原始数据
    
    def _interpolate_data_point(self, raw_stats: list, target_time: datetime, numeric_fields: list) -> Optional[Dict[str, Any]]:
        """
        为指定时间点插值数据
        
        Args:
            raw_stats: 原始统计数据列表（已排序）
            target_time: 目标时间点
            numeric_fields: 需要插值的数值字段列表
        
        Returns:
            插值后的数据点
        """
        try:
            # 查找最接近的前后两个数据点
            before_point = None
            after_point = None
            
            for i, stat in enumerate(raw_stats):
                stat_time = stat['timestamp']
                
                if stat_time <= target_time:
                    before_point = stat
                elif stat_time > target_time:
                    after_point = stat
                    break
            
            # 如果没有找到合适的数据点，使用最近的点
            if before_point is None and after_point is None:
                return None
            elif before_point is None:
                # 只有后面的点，直接使用
                result = after_point.copy()
                result['timestamp'] = target_time.replace(microsecond=0)
                return result
            elif after_point is None:
                # 只有前面的点，直接使用
                result = before_point.copy()
                result['timestamp'] = target_time.replace(microsecond=0)
                return result
            
            # 计算时间差和权重
            before_time = before_point['timestamp']
            after_time = after_point['timestamp']
            
            # 如果时间点完全匹配，直接返回
            if before_time == target_time:
                return before_point.copy()
            if after_time == target_time:
                return after_point.copy()
            
            # 计算插值权重
            total_diff = (after_time - before_time).total_seconds()
            if total_diff == 0:
                # 两个点时间相同，使用前一个点
                result = before_point.copy()
                result['timestamp'] = target_time.replace(microsecond=0)
                return result
            
            target_diff = (target_time - before_time).total_seconds()
            weight = target_diff / total_diff
            
            # 执行线性插值
            result = {'timestamp': target_time.replace(microsecond=0)}  # 确保时间戳取整到秒
            
            for field in numeric_fields:
                if field in before_point and field in after_point:
                    before_val = float(before_point[field]) if before_point[field] is not None else 0.0
                    after_val = float(after_point[field]) if after_point[field] is not None else 0.0
                    
                    # 线性插值
                    interpolated_val = before_val + (after_val - before_val) * weight
                    result[field] = round(interpolated_val, 4)  # 保留4位小数
                else:
                    # 如果字段不存在，使用前一个点的值
                    result[field] = before_point.get(field, 0.0)
            
            return result
            
        except Exception as e:
            self.logger.exception(f"Error interpolating data point: {e}")
            return None

    def get_recent_stats(self, user_group: list, time_delta: int = 5, time_interval: int = 1):
        """获取最近的统计数据"""
        try:
            if not any(group in user_group for group in ['streamer', 'manager', 'admin']):
                return {"success": False, "message": "权限不足，无法获取统计数据"}
            
            # 获取原始数据，多获取一些以便插值
            raw_stats = self.db.get_system_stats(time_delta + 5)  # 多获取60秒数据用于插值
            if not raw_stats:
                return {"success": False, "message": "没有找到相关的统计数据"}
            
            # 处理数据，按指定间隔进行插值和过滤
            processed_stats = self._process_stats_data(raw_stats, time_delta, time_interval)
            
            # 转换时间戳为字符串格式，便于JSON序列化
            for stat in processed_stats:
                if isinstance(stat['timestamp'], datetime):
                    stat['timestamp'] = stat['timestamp'].isoformat()
            
            return {
                "success": True, 
                "system_stats": processed_stats,
                "metadata": {
                    "time_delta": time_delta,
                    "time_interval": time_interval,
                    "data_points": len(processed_stats),
                    "original_points": len(raw_stats)
                }
            }
        
        except Exception as e:
            self.logger.exception(f"Error in get_recent_stats: {e}")
            return {"success": False, "message": "服务器内部错误"}

    # ============================================================================
    # Streams Information Tracking and Processing
    # ============================================================================

    def _fetch_streams_data(self):
        """获取streams数据并处理stream状态"""
        try:
            # 发送HTTP请求获取SRS streams数据
            response = requests.get(self.streams_api_url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # 检查返回状态
            if data.get('code') != 0:
                self.logger.error(f"SRS Streams API returned error code: {data.get('code')}")
                return False
            
            # 处理streams数据
            success = self._process_streams_data(data)
            if success:
                self.logger.debug("Successfully processed streams data")
                return True
            else:
                self.logger.error("Failed to process streams data")
                return False
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"HTTP request failed for streams API: {e}")
            return False
        except Exception as e:
            self.logger.exception(f"Unexpected error in fetch_streams_data: {e}")
            return False

    def _process_streams_data(self, api_data: Dict[str, Any]) -> bool:
        """处理streams API返回的数据并更新数据库"""
        try:            
            current_time = datetime.now()
            streams_data = api_data.get('streams', [])
            
            # 获取当前活跃的stream codes
            active_stream_codes = set()

            current_streams = self.db.get_stream_by_vis_raw(
                stream_visibility=['public', 'private', 'unlisted'], 
                stream_status=['planned', 'pausing']
            )
            if current_streams is None: current_streams = []
            
            # 处理每个活跃的stream
            for stream in streams_data:
                try:
                    # 检查是否为有效的live stream
                    url = stream.get('url', '')
                    publish_info = stream.get('publish', {})
                    is_active = publish_info.get('active', False)
                    
                    # 提取stream_code (格式: /live/xxx-xxxx)
                    if not url.startswith('/live/') or not is_active:
                        continue
                        
                    stream_code = url[6:]  # 去掉 '/live/' 前缀
                    
                    # 验证stream_code格式 (xxx-xxxx)
                    if '-' not in stream_code or len(stream_code.split('-')) != 2:
                        continue

                    active_stream_codes.add(stream_code)

                    # 检查stream_code是否在planned streams中
                    if not any(planned_stream['stream_code'] == stream_code for planned_stream in current_streams):
                        continue
                    
                    # 构建quality_info
                    quality_info = []
                    video_info = stream.get('video', {})
                    audio_info = stream.get('audio', {})
                    
                    if video_info:
                        quality_info.append({
                            'type': 'video',
                            'codec': video_info.get('codec'),
                            'profile': video_info.get('profile'),
                            'level': video_info.get('level'),
                            'width': video_info.get('width'),
                            'height': video_info.get('height')
                        })
                    
                    if audio_info:
                        quality_info.append({
                            'type': 'audio',
                            'codec': audio_info.get('codec'),
                            'sample_rate': audio_info.get('sample_rate'),
                            'channel': audio_info.get('channel'),
                            'profile': audio_info.get('profile')
                        })
                    
                    # Find the specific stream from the list to check its status
                    planned_stream_info = [s for s in current_streams if s['stream_code'] == stream_code]
                    if planned_stream_info[0]['stream_status'] == 'planned':
                        # 如果是开始直播就立刻获取一张以作为初始快照
                        self._capture_stream_snapshot(stream_code)
                        
                        # 记录直播开始前已存在的segment文件
                        recording_dir = Path(f"./streams_record/{stream_code}")
                        if recording_dir.exists():
                            existing_segments = {seg.name for seg in recording_dir.glob("*.mp4")}
                            if existing_segments:
                                self.stream_start_segments[stream_code] = existing_segments
                                self.logger.info(f"Recorded {len(existing_segments)} pre-existing segments for stream {stream_code}")
                    
                    # 更新stream状态为streaming
                    result = self.db.update_stream_status(stream_code, quality_info, 'streaming')
                    if result:
                        self.logger.info(f"Updated stream {stream_code} to streaming")

                except Exception as e:
                    self.logger.exception(f"Error processing individual stream: {e}")
                    continue
            
            # 处理不活跃的streams (切换为pausing状态)
            self._handle_inactive_streams(active_stream_codes, current_time)
            
            return True
            
        except Exception as e:
            self.logger.exception(f"Failed to process streams data: {e}")
            return False

    def _handle_inactive_streams(self, active_stream_codes: set, current_time: datetime):
        """处理不活跃的streams"""
        try:            
            # 获取当前streaming状态的streams
            streaming_streams = self.db.get_stream_by_vis_raw(
                stream_visibility=['public', 'private', 'unlisted'], 
                stream_status=['streaming']
            )
            
            if streaming_streams:
                for stream in streaming_streams:
                    stream_code = stream['stream_code']

                    # 如果stream不在活跃列表中，标记为pausing
                    if stream_code not in active_stream_codes:
                        # 更新为pausing状态
                        result = self.db.update_stream_status(stream_code, stream_status='pausing')
                        if result:
                            self.logger.info(f"Stream {stream_code} set to pausing (not active)")
                            # 记录pausing时间
                            self.last_seen_streams[stream_code] = current_time
            
            # 处理pausing状态的streams，检查是否超时
            pausing_streams = self.db.get_stream_by_vis_raw(
                stream_visibility=['public', 'private', 'unlisted'], 
                stream_status=['pausing']
            )

            if pausing_streams:
                for stream in pausing_streams:
                    stream_code = stream['stream_code']

                    if stream_code in self.last_seen_streams:
                        time_since_last_seen = current_time - self.last_seen_streams[stream_code]

                        if time_since_last_seen >= timedelta(seconds=self.stream_timeout_seconds):
                            # 超时，设置为ended
                            result = self.db.update_stream_status(stream_code, stream_status='ended')
                            if result:
                                self.logger.info(f"Stream {stream_code} ended due to timeout ({self.stream_timeout_seconds}s)")
                                # 清理记录
                                del self.last_seen_streams[stream_code]
                                # 合并录像段
                                self._merge_stream_recordings(stream_code)

                                # stream_start_segments记录在_merge_stream_recordings中清除
                                # del self.stream_start_segments[stream_code]
                    else: 
                        self.last_seen_streams[stream_code] = current_time
            
        except Exception as e:
            self.logger.exception(f"Error handling inactive streams: {e}")
    
    def manual_terminate_stream(self, stream_code: str) -> bool:
        """手动终止指定的stream"""
        try:
            # 检查stream是否存在
            stream = self.db.get_stream_by_code(stream_code)
            if not stream:
                self.logger.error(f"Stream {stream_code} not found")
                return False
            
            # 如果是streaming状态，更新为ended
            if stream['stream_status'] == 'streaming':
                result = self.db.update_stream_status(stream_code, stream_status='ended')
                if result:
                    self.logger.info(f"Stream {stream_code} ended due to manual termination")
                    # 合并录像段（异步执行）
                    threading.Thread(target=self._merge_stream_recordings,args=(stream_code,),daemon=True).start()
                    return True
                else: return False
            else: return False
            
        except Exception as e:
            self.logger.exception(f"Error in manual_terminate_stream: {e}")
            return False

    # ============================================================================
    # Stream Snapshot and Recording Management
    # ============================================================================

    @dataclass
    class SnapshotQuality:
        """快照质量评估结果"""
        file_path: str
        score: float
        sharpness: float
        brightness: float
        contrast: float
        file_size: int
        timeliness_bonus: float

    def _capture_all_stream_snapshots(self) -> bool:
        """捕获所有活跃流的快照"""
        try:
            # 获取所有streaming状态的streams
            streaming_streams = self.db.get_stream_by_vis_raw(
                stream_visibility=['public', 'private', 'unlisted'], 
                stream_status=['streaming']
            )
            
            if not streaming_streams:
                self.logger.debug("No streaming streams found for snapshot capture")
                return True
            
            stream_codes = [stream['stream_code'] for stream in streaming_streams]
            self.logger.info(f"Capturing snapshots for {len(stream_codes)} streams")
            
            # 使用线程池并行处理快照捕获
            success_count = 0
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_stream = {
                    executor.submit(self._capture_stream_snapshot, stream_code): stream_code 
                    for stream_code in stream_codes
                }
                
                for future in as_completed(future_to_stream):
                    stream_code = future_to_stream[future]
                    try:
                        success = future.result(timeout=30)  # 30秒超时
                        if success:
                            success_count += 1
                        else:
                            self.logger.warn(f"Failed to capture snapshot for stream {stream_code}")
                    except Exception as e:
                        self.logger.exception(f"Error capturing snapshot for stream {stream_code}: {e}")
            
            self.logger.info(f"Successfully captured {success_count}/{len(stream_codes)} snapshots")
            return success_count > 0
            
        except Exception as e:
            self.logger.exception(f"Error in capture_all_stream_snapshots: {e}")
            return False

    def _capture_stream_snapshot(self, stream_code: str) -> bool:
        """为指定的流捕获快照"""
        try:
            # 创建目录
            snapshot_dir = Path(f"./streams_record/{stream_code}")
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成时间戳文件名
            now = datetime.now()
            timestamp_filename = f"snapshot.{now.strftime('%Y.%m.%d.%H.%M.%S')}.jpg"
            timestamped_path = snapshot_dir / timestamp_filename
            best_snapshot_path = snapshot_dir / "snapshot.jpg"
            
            # RTMP URL
            rtmp_url = f"rtmp://localhost:1935/live/{stream_code}"
            
            # 使用 ffmpeg 捕获快照
            success = self._ffmpeg_capture_snapshot(rtmp_url, str(timestamped_path))
            if not success:
                return False
            
            # 分析并选择最佳快照
            self._update_best_snapshot(str(snapshot_dir), str(best_snapshot_path))
            
            self.logger.debug(f"Successfully captured snapshot for stream {stream_code}")
            return True
            
        except Exception as e:
            self.logger.exception(f"Error capturing snapshot for stream {stream_code}: {e}")
            return False

    def _ffmpeg_capture_snapshot(self, rtmp_url: str, output_path: str) -> bool:
        """使用 ffmpeg 捕获 RTMP 流的快照"""
        try:
            # ffmpeg 命令
            cmd = [
                self.ffmpeg_binary,
                '-i', rtmp_url,
                '-vframes', '1',          # 只捕获一帧
                '-q:v', '2',              # 高质量
                '-f', 'image2',           # 图像格式
                '-y',                     # 覆盖现有文件
                output_path
            ]
            
            # 执行 ffmpeg 命令，设置超时
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,  # 15秒超时
                cwd=os.getcwd()
            )
            
            if result.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                if file_size > 1024:  # 至少1KB
                    self.logger.debug(f"Successfully captured snapshot: {output_path}")
                    return True
                else:
                    self.logger.warn(f"Captured file too small: {file_size} bytes")
                    # 删除过小的文件
                    try:
                        os.remove(output_path)
                    except:
                        pass
                    return False
            else:
                self.logger.error(f"ffmpeg failed: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"ffmpeg timeout for URL: {rtmp_url}")
            return False
        except Exception as e:
            self.logger.exception(f"Error in ffmpeg capture: {e}")
            return False

    def _analyze_image_quality(self, image_path: str) -> Optional[SnapshotQuality]:
        """分析图像质量"""
        try:
            if not os.path.exists(image_path):
                return None
            
            # 读取图像
            img = cv2.imread(image_path)
            if img is None:
                return None
            
            # 转换为灰度图
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # 计算清晰度 (Laplacian variance)
            sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
            
            # 计算亮度 (平均像素值)
            brightness = np.mean(gray)
            
            # 计算对比度 (标准差)
            contrast = np.std(gray)
            
            # 获取文件大小和时间信息
            file_size = os.path.getsize(image_path)
            file_mtime = os.path.getmtime(image_path)
            current_time = time.time()
            
            # 计算时效性加成 (最近10分钟内的文件给予加成)
            time_diff = current_time - file_mtime
            max_time_bonus = 600  # 10分钟
            timeliness_bonus = max(0, (max_time_bonus - time_diff) / max_time_bonus * 10)  # 最多10分加成
            
            # 计算综合评分
            # 归一化各项指标 (0-100 分制)
            sharpness_score = min(100, max(0, sharpness / 100))
            brightness_score = 100 - abs(brightness - 128) / 128 * 100  # 128是理想亮度
            contrast_score = min(100, max(0, contrast / 64))  # 64是理想对比度
            size_score = min(100, max(0, file_size / 10240))  # 10KB 作为基准
            
            # 综合评分 (加权平均 + 时效性加成)
            total_score = (
                sharpness_score * 0.4 +  # 清晰度权重最高
                brightness_score * 0.25 + 
                contrast_score * 0.25 + 
                size_score * 0.1 +
                timeliness_bonus  # 时效性加成
            )
            
            return self.SnapshotQuality(
                file_path=image_path,
                score=total_score,
                sharpness=sharpness,
                brightness=brightness,
                contrast=contrast,
                file_size=file_size,
                timeliness_bonus=timeliness_bonus
            )
            
        except Exception as e:
            self.logger.exception(f"Error analyzing image quality for {image_path}: {e}")
            return None

    def _update_best_snapshot(self, snapshot_dir: str, best_snapshot_path: str):
        """更新最佳快照"""
        try:
            # 获取目录中所有带时间戳的快照文件
            snapshot_files = []
            for file_path in Path(snapshot_dir).glob("snapshot.*.jpg"):
                if file_path.name != "snapshot.jpg":  # 排除最佳快照文件
                    snapshot_files.append(str(file_path))
            
            if not snapshot_files:
                return
            
            # 按修改时间排序，只保留最近的10张
            snapshot_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            recent_snapshots = snapshot_files[:10]  # 只取最近的10张
            
            # 分析最近10张快照的质量
            quality_results = []
            for file_path in recent_snapshots:
                quality = self._analyze_image_quality(file_path)
                if quality:
                    quality_results.append(quality)
            
            if not quality_results:
                return
            
            # 找到最佳快照
            best_snapshot = max(quality_results, key=lambda x: x.score)
            
            # 如果当前没有最佳快照文件，或者新快照更好，则更新
            should_update = True
            if os.path.exists(best_snapshot_path):
                current_best = self._analyze_image_quality(best_snapshot_path)
                if current_best and current_best.score >= best_snapshot.score:
                    should_update = False
            
            if should_update:
                # 复制最佳快照
                shutil.copy2(best_snapshot.file_path, best_snapshot_path)
                self.logger.debug(f"Updated best snapshot with score {best_snapshot.score:.2f}")
                                
        except Exception as e:
            self.logger.exception(f"Error updating best snapshot: {e}")

    def _merge_stream_recordings(self, stream_code: str):
        """合并流的录像段并导出不同分辨率版本"""
        try:
            recording_dir = Path(f"./streams_record/{stream_code}")
            if not recording_dir.exists():
                self.logger.warn(f"Recording directory not found for stream {stream_code}")
                return
            
            # 获取所有 mp4 段文件
            segments = list(recording_dir.glob("*.mp4"))
            if not segments:
                self.logger.warn(f"No recording segments found for stream {stream_code}")
                return
            
            # 按文件名排序（时间顺序）
            segments.sort(key=lambda x: x.name)
            
            self.logger.info(f"Found {len(segments)} segments for stream {stream_code}, starting merge")
            
            # 过滤掉直播开始前的旧segment
            if hasattr(self, 'stream_start_segments') and stream_code in self.stream_start_segments:
                old_segments = self.stream_start_segments[stream_code]
                segments = [seg for seg in segments if seg.name not in old_segments]
                self.logger.info(f"Filtered out {len(old_segments)} pre-existing segments for stream {stream_code}")
                del self.stream_start_segments[stream_code]
            
            if not segments:
                self.logger.warn(f"No valid segments to merge for stream {stream_code}")
                return
            
            # 创建临时文件列表
            temp_list_file = recording_dir / "temp_segments.txt"
            with open(temp_list_file, 'w', encoding='utf-8') as f:
                for segment in segments:
                    f.write(f"file '{segment.resolve()}'\n")
            
            # 合并原始录像
            merged_file = recording_dir / f"{stream_code}.original.mp4"
            merge_success = self._ffmpeg_merge_segments(str(temp_list_file.resolve()), str(merged_file.resolve()))
            
            # 清理临时文件
            try:
                temp_list_file.unlink()
            except:
                pass

            if merge_success:
                # 导出不同分辨率版本
                self._export_resolution_versions(str(merged_file.resolve()), str(recording_dir.resolve()), stream_code)
                
                # 删除合并用的segment源文件
                for segment in segments:
                    try:
                        segment.unlink()
                        self.logger.debug(f"Deleted segment file: {segment}")
                    except Exception as e:
                        self.logger.warn(f"Failed to delete segment {segment}: {e}")
                
                self.db.update_stream_status(stream_code, stream_status='replay')
                self.logger.info(f"Successfully merged recordings for stream {stream_code} and cleaned up {len(segments)} source segments")
            else:
                self.logger.error(f"Failed to merge recordings for stream {stream_code}")

        except Exception as e:
            self.logger.exception(f"Error merging stream recordings for {stream_code}: {e}")

    def _ffmpeg_merge_segments(self, segments_list_file: str, output_file: str) -> bool:
        """使用 ffmpeg 合并录像段"""
        try:
            cmd = [
                self.ffmpeg_binary,
                '-f', 'concat',
                '-safe', '0',
                '-i', segments_list_file,
                '-c', 'copy',  # 不重新编码，直接复制
                '-y',  # 覆盖现有文件
                output_file
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5分钟超时
                cwd=os.path.dirname(segments_list_file)
            )
            
            if result.returncode == 0 and os.path.exists(output_file):
                self.logger.debug(f"Successfully merged segments to {output_file}")
                return True
            else:
                self.logger.error(f"ffmpeg merge failed: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"ffmpeg merge timeout for {output_file}")
            return False
        except Exception as e:
            self.logger.exception(f"Error in ffmpeg merge: {e}")
            return False

    def _export_resolution_versions(self, source_file: str, output_dir: str, stream_code: str):
        """导出不同分辨率版本"""
        resolutions = {
            '2K': {'width': 2560, 'height': 1440, 'bitrate': '8000k'},
            '1080p': {'width': 1920, 'height': 1080, 'bitrate': '5000k'},
            '720p': {'width': 1280, 'height': 720, 'bitrate': '3000k'}
        }
        
        for res_name, config in resolutions.items():
            try:
                output_file = os.path.join(output_dir, f"{stream_code}.{res_name}.mp4")
                
                cmd = [
                    self.ffmpeg_binary,
                    '-i', source_file,
                    '-vf', f"scale={config['width']}:{config['height']}:force_original_aspect_ratio=decrease,pad={config['width']}:{config['height']}:(ow-iw)/2:(oh-ih)/2",
                    '-c:v', 'libx264',
                    '-preset', 'medium',
                    '-crf', '23',
                    '-b:v', config['bitrate'],
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-movflags', '+faststart',
                    '-y',
                    output_file
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10分钟超时
                    cwd=output_dir
                )
                
                if result.returncode == 0 and os.path.exists(output_file):
                    file_size = os.path.getsize(output_file)
                    self.logger.info(f"Successfully exported {res_name} version: {output_file} ({file_size} bytes)")
                else:
                    self.logger.error(f"Failed to export {res_name} version: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                self.logger.error(f"Timeout exporting {res_name} version for {stream_code}")
            except Exception as e:
                self.logger.exception(f"Error exporting {res_name} version: {e}")
    
    def _resolve_ffmpeg_binary(self, preferred: Optional[str]) -> str:
        """确定 ffmpeg 可执行文件路径"""
        candidate = (preferred or "./objs/ffmpeg/bin/ffmpeg").strip()

        if candidate and candidate != 'ffmpeg':
            if os.path.sep in candidate or candidate.startswith('.'):
                candidate_path = os.path.abspath(candidate)
            else:
                candidate_path = candidate

            if os.path.exists(candidate_path):
                return candidate_path

            self.logger.warn(f"Preferred ffmpeg binary '{candidate}' not found, falling back to system ffmpeg")

        system_binary = shutil.which('ffmpeg')
        if system_binary:
            return system_binary

        return 'ffmpeg'