"""评测与门禁（技术方案第 11 章）。

四个评测维度（11.2）：
  ① 事实正确性  ② 依据正确性  ③ **过程正确性**（专业域独有）  ④ 表达适当性

其中"过程正确性"最容易被漏掉：**答案对但过程错，在财税里同样不可接受**。

门禁是分域的（8.3），一票否决项必须 100%：
  财税：效力状态正确率、引用真实存在性
  筹划：红线拦截率、高风险方案误放率
"""

from app.evaluation.gate import GATE_SHADOW, GATE_OFFLINE, GateKeeper, GateResult
from app.evaluation.runner import EvalRunner, EvalRunReport
from app.evaluation.seed import generate_cases, import_cases

__all__ = [
    "EvalRunReport",
    "EvalRunner",
    "GATE_OFFLINE",
    "GATE_SHADOW",
    "GateKeeper",
    "GateResult",
    "generate_cases",
    "import_cases",
]
