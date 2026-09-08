"""Markup/Variable 保护器(ticket-005, 工程书 §22/§23)。

翻译前把原文中的标记(<color=red>…)与变量({COUNT}…)提取为占位符
(MARKUP_001 / VAR_001), LLM 只看到"安全文本", 译后按占位符还原 ——
保证标记/变量不因翻译丢失、错位或被 LLM 改写。

设计要点:
1. 占位符按出现顺序编号(同一 token 复用同一占位符, LLM 看到的语义更稳)。
2. 原文内嵌字面占位符(如游戏文本恰好含 "VAR_001")→ 编号自动顺延,
   还原永不触碰字面量(碰撞安全)。
3. 纯字符串变换, 对内容零语义解释 —— "ignore instructions" 类注入文本
   原样放行(§21: 游戏文本是不可信内容, 引擎只翻译不执行)。
4. 组合保护顺序: 先变量后标记; 还原必须逆序(标记内可嵌变量)。
"""
from __future__ import annotations

import re
from typing import Pattern


class TokenProtector:
    """通用占位符保护器: 提取一类 token → {PREFIX}NNN, 支持还原。"""

    prefix: str = "TOKEN_"
    pattern: Pattern[str] = re.compile(r"(?!)")  # 永不匹配的默认

    def protect(self, text: str) -> tuple[str, dict[str, str]]:
        """text → (protected_text, {placeholder: original_token})。

        占位符编号从左到右递增; 原文已含的字面占位符编号会被跳过,
        且同一原文 token 复用同一占位符(幂等、可逆)。
        """
        mapping: dict[str, str] = {}
        by_token: dict[str, str] = {}
        literal = set(re.findall(rf"{re.escape(self.prefix)}\d+", text))
        counter = 1

        def _sub(m: re.Match[str]) -> str:
            nonlocal counter
            token = m.group(0)
            existing = by_token.get(token)
            if existing is not None:
                return existing
            while f"{self.prefix}{counter:03d}" in literal:
                counter += 1  # 字面冲突顺延(碰撞安全)
            ph = f"{self.prefix}{counter:03d}"
            mapping[ph] = token
            by_token[token] = ph
            counter += 1
            return ph

        protected = self.pattern.sub(_sub, text)
        # 原文不含可保护 token 时, 保证返回原字符串(引用相等亦可)
        return protected, mapping

    def restore(self, text: str, mapping: dict[str, str]) -> str:
        """把占位符替换回原文 token。

        按占位符编号升序逐个替换(编号唯一, 顺序无关紧要但保证确定性);
        字面量文本(非占位符)不受影响。缺失的占位符原样保留, 由 Validator
        在还原前拦截(§24), 这里不做静默丢弃。
        """
        for ph in sorted(mapping, key=lambda p: int(p.rsplit("_", 1)[1])):
            text = text.replace(ph, mapping[ph])
        return text


class MarkupProtector(TokenProtector):
    """标记保护(§22): <color=red>、</color>、<b> 等 DF 标记。

    标记内容必须以字母或 '/' 开头 —— "30 > 20" 这类比较式不会被误判。
    """

    prefix = "MARKUP_"
    pattern = re.compile(r"</?[A-Za-z][A-Za-z0-9_=:./ -]*>")


class VariableProtector(TokenProtector):
    """变量保护(§23): {COUNT}、{UNIT_NAME}、{ITEM} 等 DF 占位变量。"""

    prefix = "VAR_"
    pattern = re.compile(r"\{[^{}\n]+\}")


class Protector:
    """组合保护器: 变量 + 标记两级保护, 还原逆序展开(§22 → §23 顺序)。"""

    def __init__(
        self,
        protectors: tuple[TokenProtector, ...] = (VariableProtector(), MarkupProtector()),
    ) -> None:
        self.protectors = protectors

    def protect(self, text: str) -> tuple[str, dict[str, str]]:
        """依次应用各子保护器; 占位符前缀互斥, 映射可合并为一张表。"""
        mapping: dict[str, str] = {}
        out = text
        for p in self.protectors:
            out, part = p.protect(out)
            mapping.update(part)
        return out, mapping

    def restore(self, text: str, mapping: dict[str, str]) -> str:
        """按保护顺序的逆序还原(标记占位符内可能嵌变量占位符)。"""
        for p in reversed(self.protectors):
            text = p.restore(text, mapping)
        return text
