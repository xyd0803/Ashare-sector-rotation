#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— Gate 通用结果类型
====================================
三道闸（行业周期量价 / 数据双源验证 / finalgate）共用的轻量结果对象。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GateResult:
    name: str
    level: str = "PASS"          # PASS / WARN / FAIL
    errors: List[str] = field(default_factory=list)      # 阻断级（须修复）
    warnings: List[str] = field(default_factory=list)   # 提示级（不阻断）
    info: List[str] = field(default_factory=list)       # 通过说明

    @property
    def ok(self) -> bool:
        return self.level != "FAIL" and len(self.errors) == 0

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        self.level = "FAIL"

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        if self.level == "PASS":
            self.level = "WARN"

    def note(self, msg: str) -> None:
        self.info.append(msg)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "level": self.level,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "info": list(self.info),
        }
