# Claude 메모리 동기화 셋업

이 폴더(`.claude-memory/`)는 Claude Code의 **프로젝트 메모리**를 git로 동기화하기 위한 곳입니다.
Claude는 메모리를 `C:\Users\<사용자>\.claude\projects\C--trading-bot\memory\` 에서 읽고 씁니다.
그 경로를 이 폴더로 **디렉토리 junction** 연결해두면, 메모리를 쓰는 즉시 저장소에 반영되어
`git push`/`git pull` 로 PC ↔ 노트북 간에 같은 기억을 공유할 수 있습니다.

## 새 기기에서 1회 셋업 (clone 직후)

PowerShell에서 (저장소가 `C:\trading-bot` 에 있다고 가정):

```powershell
$link   = "$env:USERPROFILE\.claude\projects\C--trading-bot\memory"
$target = "C:\trading-bot\.claude-memory"

# 기존 memory 폴더가 있으면(자동 생성됐을 수 있음) 내용 백업 후 제거
if (Test-Path $link) {
    $item = Get-Item $link
    if ($item.LinkType -ne 'Junction') {
        Get-ChildItem $link -ErrorAction SilentlyContinue | Move-Item -Destination $target -Force
        Remove-Item $link -Recurse -Force
    }
}
New-Item -ItemType Junction -Path $link -Target $target | Out-Null
Get-Item $link | Select-Object Name, LinkType, Target | Format-List
```

> 저장소 경로가 `C:\trading-bot` 가 아니면 `$target` 을 실제 경로로 바꾸세요.
> 프로젝트 폴더명(`C--trading-bot`)은 저장소 경로에 따라 달라질 수 있으니, 다르면 `$link` 도 맞추세요.

## 평소 운영 규칙

- **기기 바꾸기 전**: `git add -A && git commit && git push` (메모리 + 핸드오프 + 코드 전부)
- **새 기기에서 시작 시**: `git pull` → 메모리/핸드오프 자동 반영
- junction은 git에 저장되지 않으므로 **기기마다 위 셋업을 1회**씩 해야 합니다.

## 주의

- junction의 **대상 파일들**(이 폴더 내용)만 git로 추적됩니다. junction 링크 자체는 추적 안 됩니다.
- `MEMORY.md` 는 메모리 인덱스입니다. 직접 편집보다 Claude가 갱신하도록 두세요.
