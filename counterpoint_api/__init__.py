# -*- coding: utf-8 -*-
"""离线两声部 MusicXML 对位作业审阅 API。

模块组成::

    rulesconfig   规则配置（拍号强弱、协和音程、音域、跳进）
    musicxml_io   MusicXML 解析（按 divisions 还原时间，不做任何补全）
    counterpoint  对位分析引擎（按拍检查）
    storage       SQLite 持久层（原谱、规则版本、裁定）
    server        http.server 路由与接口

只依赖 Python 标准库。
"""

__all__ = ["rulesconfig", "musicxml_io", "counterpoint", "storage", "server"]
__version__ = "1.0.0"
