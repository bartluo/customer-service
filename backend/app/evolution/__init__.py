"""自进化引擎（技术方案第 8 章）。

一句话说清这一层存在的理由：
  **财税知识的过期不是因为"没人用"，而是因为政策改了。**
  所以必须有主动的变更监控，而不是等用户发现答案不对。

四类流水线：
  A 资料摄入   —— 新文件 → 结构化条文 → 知识单元
  B 对话反哺   —— 从专家复核通过的回答里提炼
  C 缺口发现   —— 低置信 + 未命中问题聚类
  D 衰退淘汰   —— health_score 打分与复审队列

财税域特有（8.2）：法规变更监控是整个进化的核心来源。
"""

from app.evolution.gaps import GapFinder, record_gap
from app.evolution.health import HealthScorer
from app.evolution.impact import ImpactAnalysis, analyze_impact
from app.evolution.monitor import MonitorScan, OfficialMonitor
from app.evolution.notify import build_change_digest

__all__ = [
    "GapFinder",
    "HealthScorer",
    "ImpactAnalysis",
    "MonitorScan",
    "OfficialMonitor",
    "analyze_impact",
    "build_change_digest",
    "record_gap",
]
