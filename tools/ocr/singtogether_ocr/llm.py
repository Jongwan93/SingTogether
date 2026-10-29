"""사용자 컴퓨터에 로그인된 AI CLI로 악보 그림을 받아 적게 한다. 앱은 시작할 때 둘 중 하나를 고르게 한다.

    claude  Claude Code CLI (`claude`)              모델: SINGTOGETHER_CLAUDE_MODEL (기본 sonnet)
    gemini  Antigravity (`agy`), 없으면 Gemini CLI (`gemini`, API 키 필요)

앱을 Finder에서 열면 PATH가 짧아서 흔한 설치 폴더를 직접 뒤진다. "auto"는 위 순서로 처음 찾은 것.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .engines._run import run

PROVIDERS = ("claude", "gemini")
CHOICES = ("auto", *PROVIDERS, "none")
LABELS = {"claude": "Claude", "gemini": "Gemini", "none": "Apple Vision"}
_BINARIES = {"claude": ("claude",), "gemini": ("agy", "gemini")}
_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.npm-global/bin", "~/.bun/bin",
         "~/.volta/bin", "~/.claude/local")


def find(name: str) -> str | None:
    if found := shutil.which(name):
        return found
    for d in _DIRS:
        p = Path(d).expanduser() / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    nvm = sorted((Path.home() / ".nvm/versions/node").glob(f"*/bin/{name}"))
    return str(nvm[-1]) if nvm else None


def binary(provider: str) -> tuple[str, str] | None:
    """(실행 파일 이름, 경로). gemini는 agy를 먼저 찾는다."""
    for name in _BINARIES.get(provider, ()):
        if path := find(name):
            return name, path
    return None


def pick(pref: str = "auto") -> str:
    """쓸 제공자 이름. 고른 것이 설치돼 있지 않으면 "none"."""
    if pref == "none":
        return "none"
    if pref == "auto":
        return next((p for p in PROVIDERS if binary(p)), "none")
    return pref if binary(pref) else "none"


def available() -> dict[str, str]:
    """설치된 제공자 → 실행 파일 경로 (앱 메뉴 표시용)."""
    return {p: b[1] for p in PROVIDERS if (b := binary(p))}


def check(provider: str, timeout: int = 45) -> dict:
    """설치·로그인 상태. {"provider", "installed", "signed_in", "message"}. 앱이 시작할 때와 AI를 고를 때 부른다.
    claude는 `claude auth status`(0.1초), agy는 상태 명령이 없어 한 단어 답을 시켜 본다(약 7초)."""
    import subprocess
    found = binary(provider)
    label = LABELS.get(provider, provider)
    if not found:
        return {"provider": provider, "installed": False, "signed_in": False, "message": f"{label} CLI is not installed"}
    name, path = found
    try:
        if name == "claude":
            p = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=timeout)
            out = p.stdout.strip()
            ok = bool(json.loads(out[out.index("{"):]).get("loggedIn")) if "{" in out else False
            hint = "Open Terminal and run: claude"
        elif name == "agy":
            p = subprocess.run([path, "--print-timeout", f"{timeout - 5}s", "-p=Reply with exactly the word OK and nothing else."],
                               capture_output=True, text=True, timeout=timeout)
            out = p.stdout + p.stderr
            ok = p.returncode == 0 and "OK" in out and not re.search(r"sign in|log ?in|unauthorized|not authenticated", out, re.I)
            hint = "Open Terminal and run: agy"
        else:
            p = subprocess.run([path, "--approval-mode", "plan", "-p", "Reply with exactly the word OK."],
                               capture_output=True, text=True, timeout=timeout)
            out = p.stdout + p.stderr
            ok = p.returncode == 0 and "OK" in out and not re.search(r"IneligibleTierError|Error authenticating", out)
            hint = "Set GEMINI_API_KEY or install Antigravity (agy)"
    except Exception as e:   # 시간 초과 등
        return {"provider": provider, "installed": True, "signed_in": False, "message": f"{label} did not answer ({type(e).__name__})"}
    return {"provider": provider, "installed": True, "signed_in": ok,
            "message": f"{label} is signed in" if ok else f"{label} is installed but not signed in. {hint}"}


def ask(provider: str, prompt: str, image: Path, timeout: int = 600) -> str:
    """그림 한 장과 지시문을 주고 답 글을 받는다. 지시문의 {image}는 그림의 절대 경로로 바뀐다."""
    found = binary(provider)
    if not found:
        raise RuntimeError(f"{LABELS.get(provider, provider)} CLI not found")
    name, path = found
    image = image.resolve()
    prompt = prompt.format(image=image)
    if name == "claude":
        model = os.environ.get("SINGTOGETHER_CLAUDE_MODEL", "sonnet")
        _, out = run([path, "-p", prompt, "--model", model, "--tools", "Read", "--allowedTools", "Read",
                      "--output-format", "json"], timeout=timeout)
        result = json.JSONDecoder().raw_decode(out[out.index("{"):])[0]
        if result.get("is_error"):
            raise RuntimeError(f"claude failed: {result.get('result')}")
        return result["result"]
    if name == "agy":
        # 헤드리스 모드에서는 프롬프트를 플래그에 붙여야 하고, 셸 명령을 쓰려 하면 권한을 못 받아 빈 결과가 나온다 (프롬프트로 막음)
        _, out = run([path, "--add-dir", str(image.parent), "--print-timeout", f"{max(60, timeout - 60)}s",
                      "-p=" + prompt], timeout=timeout)
        return out
    # Gemini CLI: @경로로 그림을 붙인다. plan 모드는 읽기 전용.
    _, out = run([path, "--approval-mode", "plan", "--skip-trust", "--include-directories", str(image.parent),
                  "-p", prompt + f" @{image}"], timeout=timeout)
    if re.search(r"IneligibleTierError|Error authenticating", out):
        raise RuntimeError("Gemini CLI is not signed in (set GEMINI_API_KEY or use agy)")
    return "\n".join(l for l in out.splitlines() if not re.match(r"(Warning:|\[STARTUP\]|Loaded cached)", l))
