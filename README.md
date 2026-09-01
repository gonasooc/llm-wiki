# llm-wiki

YouTube 링크 하나를 Discord 리서치 스레드로 조사하고, 인용이 검증된 한국어 리포트를 만든 뒤, 사용자가 명시적으로 요청할 때만 Obsidian 초안으로 발행하는 Hermes 에이전트용 스킬·플러그인 소스입니다.

이 저장소는 **소스만** 담고 있습니다. 실행은 Hermes Gateway가 하며, Discord 봇 토큰 등 크리덴셜은 이 저장소가 아니라 Gateway 프로필(`$HERMES_HOME`)에 있습니다.

## 구성

| 구성 요소 | 경로 | 역할 |
| --- | --- | --- |
| `hermes-wiki` 스킬 | `.hermes/skills/hermes-wiki/` | 리서치 절차, 출력 형식, 발행 규칙을 정의하는 에이전트 지침 |
| `hermes-wiki-router` 플러그인 | `.hermes/plugins/hermes-wiki-router/` | 같은 영상 링크를 기존 Discord 스레드로 되돌려 보내는 라우터 |
| 발행 헬퍼 | `.hermes/skills/hermes-wiki/scripts/publish_note.py` | 검증을 통과한 초안을 Obsidian vault에 안전하게 기록하는 CLI |
| 테스트 | `tests/` | 플러그인 라우팅·발행 헬퍼·스킬 계약 검증 |

## 동작 흐름

1. **스레드 배치** — 부모 채널에서 봇을 멘션하면 라우터가 `gateway_message_route` 훅으로 개입합니다. 메시지에 YouTube 영상 ID가 하나 있으면 `(프로필, 길드, 부모 채널, 영상 ID)`를 키로 SQLite에 저장된 스레드를 찾아 **재사용**하고, 없으면 새로 만듭니다. 같은 영상을 다시 올려도 스레드가 늘어나지 않습니다.
2. **리서치** — 스킬이 영상 하나만 받아들이고(복수 링크는 거절), 정체성 확인 → transcript 확보 → 공식·1차 출처 우선 수집(10~20개 목표) 순으로 진행합니다. transcript가 없어도 실패로 보지 않고 그 사실을 명시합니다.
3. **인용 원장** — 모든 출처는 URL을 수집하는 시점에 `grounded-citations`의 원장(ledger)에 등록됩니다. 최종 출처 목록은 손으로 쓰지 않고 원장에서 기계적으로 렌더링한 뒤 strict 검증을 통과해야 합니다.
4. **리포트** — 한국어로 작성하되 고유명사와 짧은 인용은 원어를 유지합니다. **검증된 사실 / 출처의 해석 / Hermes의 분석**을 섞지 않고 구분하며, 근거를 찾지 못한 주장은 `[unverified]`로 남깁니다.
5. **발행(선택)** — `Obsidian에 저장해줘` 같은 명시적 요청이 있을 때만 vault에 씁니다. `글로 정리해줘`는 채팅 전용 초안 요청이며 파일 쓰기 권한으로 해석하지 않습니다.

## 설계 원칙

- **신뢰 경계** — transcript, 설명, 댓글, 검색된 페이지는 전부 신뢰할 수 없는 데이터로 취급합니다. 그 안에 담긴 지시는 따르지 않으며, 행동을 승인할 수 있는 것은 사용자의 Discord 메시지와 Hermes 지침뿐입니다.
- **생성 전용 발행** — 발행 헬퍼에는 갱신 모드가 없습니다. 같은 영상의 노트가 이미 있으면 덮어쓰지 않고 거절합니다. 동시 편집으로 인한 조용한 데이터 손실보다 수동 처리를 택한 결정입니다.
- **경로 안전성** — 헬퍼는 심볼릭 링크를 따라가지 않는 디렉터리 디스크립터 방식으로 vault 경로를 열고, 원자적 no-overwrite 생성으로 노트를 씁니다. 해당 POSIX 원시 기능이 없는 호스트에서는 발행이 실패하도록(fail closed) 동작합니다.
- **셸 주입 차단** — 제목·설명·태그는 셸 인자로 넘기지 않고 JSON 요청 파일로 전달합니다.

## 설치

플러그인을 Hermes 프로필로 복사한 뒤 활성화합니다.

```bash
PLUGIN_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$PLUGIN_HOME/plugins"
cp -R .hermes/plugins/hermes-wiki-router "$PLUGIN_HOME/plugins/"
HERMES_HOME="$PLUGIN_HOME" hermes plugins enable hermes-wiki-router --no-allow-tool-override
```

부모 채널을 지정하고, Discord 네이티브 어댑터가 먼저 스레드를 만들지 않도록 같은 채널을 `no_thread_channels`에 넣습니다. `<PARENT_CHANNEL_ID>`는 실제 채널 ID로 바꾸세요.

```bash
hermes config set plugins.entries.hermes-wiki-router.settings.parent_channel_id <PARENT_CHANNEL_ID>
hermes config set plugins.entries.hermes-wiki-router.allow_platform_actions true
hermes config set discord.no_thread_channels '["<PARENT_CHANNEL_ID>"]'
hermes config set discord.auto_thread_mentions_in_free_response false
```

> `no_thread_channels`는 리스트 값이라 `config set`이 목록 전체를 교체합니다. 기존 값이 있으면 `hermes config get`으로 확인한 뒤 기존 ID를 모두 포함해 넘겨야 합니다.

자세한 배경은 `.hermes/plugins/hermes-wiki-router/README.md`를 참고하세요.

## 설정

- **스레드 매핑 DB** — `$HERMES_HOME/plugin-data/hermes-wiki-router/youtube_threads.db` (프로필 소유, `db_path`로 변경 가능)
- **vault 경로** — `$HERMES_HOME/plugin-data/hermes-wiki-router/publishing.json`에 절대 경로 `vault_path` 한 개를 담습니다. 노트는 `<vault>/articles/<video-id>.md`에 생성됩니다. 머신마다 다른 경로이므로 저장소에 커밋하지 않습니다.
- **인용 원장 캐시** — `$HERMES_HOME/cache/citations/hermes-wiki/<thread-id>/<video-id>.json`

## 테스트

```bash
uv run --with pytest --with pytest-asyncio --with pyyaml python -m pytest tests -q
```

현재 43개 중 42개가 통과합니다. `test_default_db_path_honors_profile_home_override` 하나는 Hermes 런타임의 `hermes_constants` 모듈을 임포트하므로, Hermes가 설치되지 않은 환경에서는 실패합니다.

## 라이선스

MIT
