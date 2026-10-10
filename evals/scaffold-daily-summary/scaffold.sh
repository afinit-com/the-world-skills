#!/usr/bin/env bash
# Fixture: a directory of dropped files for the bot to watch. The original version of this
# case asked for a git-log summary, which cannot work here: the eval sandbox has no usable
# git (`xcrun: Failed to locate 'git'`, measured), so the job was impossible for
# reasons that have nothing to do with the skill under test. A watched folder is the same
# shape of job — notice what is new since last time, summarize it, stay inside your scope —
# without depending on a tool the sandbox lacks.
#
# Referenced from case.yaml as `scaffold_script: scaffold.sh`, a PATH, not inline bash.
set -eu

mkdir -p inbox

cat > inbox/2026-09-17-deploy.md <<'MD'
# 배포 기록 2026-09-17
- api 서버 v2.3.1 롤아웃, 카나리 10% → 100%
- 롤백 없음
MD

cat > inbox/2026-09-18-incident.md <<'MD'
# 장애 기록 2026-09-18
- 02:10 검색 페이지 응답 지연, 평균 응답 4.2s
- 원인: 캐시 서버 재시작으로 캐시가 비어 있었음. 코드 변경 없음
- 03:05 캐시가 다시 채워지며 정상화
MD

cat > inbox/README.md <<'MD'
사람이 손으로 떨어뜨리는 폴더. 하루에 0~3개쯤 새 파일이 들어온다.
MD
