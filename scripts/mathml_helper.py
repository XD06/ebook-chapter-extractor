#!/usr/bin/env python3
"""
mathml_helper.py - 轻量 MathML 转 LaTeX 转换器 (纯 Python AST 解析)
支持 EPUB 3 / HTML5 内嵌 <math> 标签的精准转换，0 外部重型依赖
"""

import re
from typing import Optional

def _clean_text(s: str) -> str:
    return (s or "").strip()

def mathml_node_to_latex(node) -> str:
    """递归将 MathML XML/Soup 节点转为 LaTeX 表达式"""
    if node is None:
        return ""

    tag_name = getattr(node, "name", None)
    if not tag_name:
        # 纯文本节点
        return _clean_text(str(node))

    # 去除命名空间 (如 m:math -> math)
    tag = tag_name.split(":")[-1].lower()

    if tag in ("mi", "mn"):
        text = _clean_text(node.get_text())
        # 特殊希腊字母映射
        greek = {
            "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
            "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
            "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
            "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
            "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
            "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
            "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
            "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
            "Ψ": r"\Psi", "Ω": r"\Omega",
        }
        return greek.get(text, text)

    elif tag == "mo":
        text = _clean_text(node.get_text())
        op_map = {
            "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "≈": r"\approx ",
            "≡": r"\equiv ", "∝": r"\propto ", "±": r"\pm ", "∓": r"\mp ",
            "×": r"\times ", "÷": r"\div ", "·": r"\cdot ", "∈": r"\in ", "∉": r"\notin ",
            "⊂": r"\subset ", "∪": r"\cup ", "∩": r"\cap ", "→": r"\to ",
            "⇒": r"\Rightarrow ", "⇔": r"\Leftrightarrow ",
            "∞": r"\infty ", "∑": r"\sum ", "∏": r"\prod ", "∫": r"\int ",
            "∂": r"\partial ", "∇": r"\nabla ",
        }
        return op_map.get(text, f" {text} " if text in ("+", "-", "=", "<", ">") else text)

    elif tag == "mtext":
        text = _clean_text(node.get_text())
        return f"\\text{{{text}}}"

    elif tag == "mspace":
        return " "

    elif tag == "msup":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            base = mathml_node_to_latex(children[0])
            exp = mathml_node_to_latex(children[1])
            return f"{base}^{{{exp}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "msub":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            base = mathml_node_to_latex(children[0])
            sub = mathml_node_to_latex(children[1])
            return f"{base}_{{{sub}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "msubsup":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 3:
            base = mathml_node_to_latex(children[0])
            sub = mathml_node_to_latex(children[1])
            exp = mathml_node_to_latex(children[2])
            return f"{base}_{{{sub}}}^{{{exp}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "mfrac":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            num = mathml_node_to_latex(children[0])
            den = mathml_node_to_latex(children[1])
            return f"\\frac{{{num}}}{{{den}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "msqrt":
        inner = "".join(mathml_node_to_latex(c) for c in node.children)
        return f"\\sqrt{{{inner}}}"

    elif tag == "mroot":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            base = mathml_node_to_latex(children[0])
            deg = mathml_node_to_latex(children[1])
            return f"\\sqrt[{deg}]{{{base}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "mover":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            base = mathml_node_to_latex(children[0])
            top = mathml_node_to_latex(children[1])
            accent_map = {"¯": r"\bar", "ˆ": r"\hat", "^": r"\hat", "~": r"\tilde", "→": r"\vec"}
            if top.strip() in accent_map:
                return f"{accent_map[top.strip()]}{{{base}}}"
            return f"\\overset{{{top}}}{{{base}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "munder":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 2:
            base = mathml_node_to_latex(children[0])
            bot = mathml_node_to_latex(children[1])
            if any(op in base for op in (r"\sum", r"\prod", r"\lim", r"\max", r"\min")):
                return f"{base}_{{{bot}}}"
            return f"\\underset{{{bot}}}{{{base}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "munderover":
        children = [c for c in node.children if getattr(c, "name", None) or _clean_text(str(c))]
        if len(children) >= 3:
            base = mathml_node_to_latex(children[0])
            bot = mathml_node_to_latex(children[1])
            top = mathml_node_to_latex(children[2])
            return f"{base}_{{{bot}}}^{{{top}}}"
        return "".join(mathml_node_to_latex(c) for c in children)

    elif tag == "mtable":
        rows_latex = []
        for r in node.find_all(lambda t: getattr(t, "name", None) and t.name.split(":")[-1].lower() == "mtr", recursive=False):
            cells = [
                mathml_node_to_latex(c)
                for c in r.find_all(lambda t: getattr(t, "name", None) and t.name.split(":")[-1].lower() == "mtd", recursive=False)
            ]
            if cells:
                rows_latex.append(" & ".join(cells))
        if rows_latex:
            return r"\begin{matrix}" + r" \\ ".join(rows_latex) + r"\end{matrix}"
        return "".join(mathml_node_to_latex(c) for c in getattr(node, "children", []))

    elif tag == "menclose":
        inner = "".join(mathml_node_to_latex(c) for c in node.children)
        if "box" in (node.get("notation") or "").lower():
            return f"\\boxed{{{inner}}}"
        return inner

    elif tag in ("mrow", "mstyle", "semantics", "math", "mpadded"):
        inner = "".join(mathml_node_to_latex(c) for c in node.children)
        if tag == "math":
            display = node.get("display") == "block" or node.get("mode") == "display"
            if display:
                return f"\n$$\n{inner.strip()}\n$$\n"
            else:
                return f"${inner.strip()}$"
        return inner

    elif tag in ("mfenced",):
        open_char = node.get("open", "(")
        close_char = node.get("close", ")")
        inner = "".join(mathml_node_to_latex(c) for c in node.children)
        return f"{open_char}{inner}{close_char}"

    else:
        # 默认递归处理所有子节点
        return "".join(mathml_node_to_latex(c) for c in getattr(node, "children", []))


def convert_soup_mathml_to_latex(soup) -> None:
    """在 BeautifulSoup DOM 树中原地替换所有 <math> 标签为 LaTeX 文本节点"""
    for math_tag in soup.find_all(["math", "m:math"]):
        latex_str = mathml_node_to_latex(math_tag)
        math_tag.replace_with(latex_str)
