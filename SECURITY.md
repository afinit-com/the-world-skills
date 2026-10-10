# Security policy

**Don't report a vulnerability in a public issue.** Use GitHub's private reporting form:
the **Security** tab of this repository → **Report a vulnerability**. It opens a thread
visible only to you and the maintainer.

If that form isn't available to you for some reason, open a public issue that says only
that you have something to report and asks for a private channel — no details, no repro.

## What counts here

This kit installs code into your machine and handles credentials, so the interesting
reports are about the parts that are supposed to hold a boundary:

- **A scope guard that can be bypassed.** An agent writing outside its `write_scope`, a
  spawned agent reaching a tool it wasn't handed, `WORLD_AGENT` being spoofable so the
  guard trusts the wrong identity.
- **The single-approver rule failing.** Anything that lets a bot originate an outward
  action instead of relaying an HMAC-signed human decision, or a forged relay that
  verifies.
- **Credential exposure.** A Slack token, a Codex or Claude login, or an approval secret
  ending up in a log line, a posted message, a run footer, a generated `WORLD.html`, or a
  file the installer leaves world-readable.
- **The installer or a flow writing outside its target directory**, or a scaffolded
  command that executes attacker-controlled input.

Out of scope: a harness *you* configured to have a wide scope is working as designed;
plugin loading (`Agent.allow_plugins` with a `PluginMarketplace`, Claude runner only) is
documented as an explicit full-trust path and is not a vulnerability; and the providers' own products (Claude Code, Codex, Slack) should
go to those vendors.

## What to expect

One person maintains this. I'll acknowledge a report and tell you whether I think it's
real; if it is, the fix and a note in the repo follow. I don't run a bounty and can't
promise a timeline. Please give me a reasonable window before publishing, and credit is
yours unless you'd rather not have it.

---

## 한국어

**취약점은 공개 이슈로 올리지 말아주세요.** 이 저장소의 **Security** 탭 →
**Report a vulnerability**로 비공개 제보가 가능합니다. 해당 폼이 보이지 않으면, 내용
없이 "제보할 것이 있으니 비공개 채널을 달라"는 이슈만 남겨주세요.

특히 의미 있는 제보는 경계를 지키기로 되어 있는 부분이 뚫리는 경우입니다 — 범위 가드
우회(`write_scope` 밖 쓰기, 넘겨주지 않은 tool 접근, `WORLD_AGENT` 위조), 단일 승인자
규칙 무력화(봇이 사람 결정을 중계하지 않고 스스로 생성, 서명 위조), 자격 증명 노출
(로그·게시 메시지·실행 푸터·`WORLD.html`·권한이 느슨한 파일), 설치 스크립트나 플로우가
대상 디렉터리 밖에 쓰는 경우.

범위 밖: 사용자가 직접 넓게 설정한 하네스 범위는 의도된 동작이고,
플러그인 로딩(`Agent.allow_plugins` + `PluginMarketplace`, Claude 러너 전용)은 완전 신뢰
경로임을 문서에 명시한 것이라 취약점이 아닙니다. 제공사
제품(Claude Code, Codex, Slack) 자체의 문제는 해당 벤더로 보내주세요.

관리자는 한 명입니다. 제보를 확인하고 실제 문제인지 회신드리며, 맞다면 수정과 기록이
따릅니다. 버그 바운티는 없고 기한을 약속드리긴 어렵습니다. 공개 전 적절한 유예를
부탁드리고, 원하지 않으신다면 별도로 말씀해 주세요 — 기본적으로는 제보자를 명시합니다.
