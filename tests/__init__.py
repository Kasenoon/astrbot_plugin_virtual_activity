"""插件测试包。

把仓库的上级目录加进 sys.path，这样在仓库根目录直接跑
`python -m unittest discover -s tests -t .` 时也能 import 到
`astrbot_plugin_virtual_activity.core`（本地开发和 GitHub Actions 都适用）。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))
