# uv 로 설치하기

`pyproject.toml` 과 `uv.lock` 이 들어 있습니다. `uv.lock` 은 실제로
`uv sync` 를 돌려 만든 것이라, 같은 버전 조합이 그대로 재현됩니다.

---

## 1. uv 설치 (한 번만)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

설치 후 **터미널을 새로 열어야** PATH 가 잡힙니다.

```powershell
uv --version
```

winget 이나 scoop 를 쓰신다면 그쪽도 됩니다.

```powershell
winget install --id=astral-sh.uv -e
```

---

## 2. 프로젝트 설치

```powershell
cd C:\dev\taekwonw-agent-v0.3
uv sync
```

이게 전부입니다. `uv` 가 알아서 처리합니다.

- Python 3.12 가 없으면 **직접 내려받아 씁니다** (`requires-python = ">=3.12"`)
- `.venv` 를 만들고
- `uv.lock` 에 적힌 정확한 버전으로 의존성을 설치합니다

`py -m venv` 나 `pip install` 을 따로 하지 않습니다. 가상환경을 activate 할
필요도 없습니다.

### 삽화까지 쓰려면

로컬 이미지 생성(`IMAGE_SOURCE=local`)은 torch 때문에 2GB 넘게 받습니다.
그래서 선택 의존성으로 뺐습니다.

```powershell
uv sync --extra images
```

Gemini 이미지 API 를 쓰실 거라면(`IMAGE_SOURCE=gemini`) 설치하지 않아도 됩니다.

> Windows 의 PyPI torch 휠은 이미 CPU 전용이라, 별도 인덱스 설정이 필요 없습니다.
> GPU 를 쓰실 거면 `pyproject.toml` 에 PyTorch 인덱스를 추가해야 합니다
> (https://docs.astral.sh/uv/guides/integration/pytorch/).

---

## 3. 자격증명

```powershell
copy .env.example .env
notepad .env
```

키 4종(네이버 2개, Gemini, Notion)을 채웁니다.

---

## 4. 실행

`uv run` 을 앞에 붙이면 됩니다. 가상환경 activate 가 필요 없습니다.

```powershell
# 형태소 분석기 첫 실행 (사전 다운로드, 1~2분)
uv run python -c "from core.korean_morphology import get_kiwi; get_kiwi()"

# API 를 쓰지 않는 경로부터 확인
uv run python run.py check --file prompts/draft/writing_guide.md

# 드라이런 (Notion 에 쓰지 않고 LLM 도 부르지 않습니다)
uv run python run.py collect --dry-run

# 실제 실행
uv run python run.py collect
```

`[project.scripts]` 를 넣어 뒀지만, `package = false` 프로젝트에서는
콘솔 스크립트가 설치되지 않습니다. `uv run python run.py ...` 형태를 쓰세요.

### 매번 `uv run` 을 붙이기 싫다면

```powershell
.venv\Scripts\activate
python run.py collect
```

기존 방식 그대로 됩니다. `uv sync` 가 만든 `.venv` 를 쓰는 것뿐입니다.

---

## 자주 쓰는 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 의존성 설치·동기화 | `uv sync` |
| 삽화 의존성 포함 | `uv sync --extra images` |
| 패키지 추가 | `uv add 패키지명` |
| 패키지 제거 | `uv remove 패키지명` |
| 설치된 버전 확인 | `uv pip list` |
| 잠금 파일 갱신 | `uv lock --upgrade` |
| 환경 초기화 | `.venv` 폴더 삭제 후 `uv sync` |

`uv add` 는 `pyproject.toml` 과 `uv.lock` 을 함께 고칩니다. `pip install` 로
직접 넣으면 잠금 파일과 어긋나 다음 `uv sync` 에서 사라집니다.

---

## uv.lock 은 커밋하세요

`.gitignore` 에서 제외해 뒀습니다. 이 파일이 있어야 다른 컴퓨터나 다시 설치할 때
같은 버전 조합이 재현됩니다. 이번처럼 프로젝트를 잃었을 때 버전을 특정할 수 있는
근거이기도 합니다.

```powershell
git init
git add .
git commit -m "복원 시점"
```

---

## 원래 버전을 되찾고 싶다면

`pyproject.toml` 의 버전 하한은 "이 버전 이후로 API 가 안정적"이라는 뜻이지,
원본에서 쓰시던 버전이 아닙니다. 원본 `requirements.txt` 가 유실돼 특정할 수
없었습니다.

옛 `.venv` 폴더가 어딘가 남아 있다면 거기서 회수할 수 있습니다.

```powershell
C:\옛경로\.venv\Scripts\python.exe -m pip freeze > old-versions.txt
```

---

## requirements.txt 는 어떻게 되나요

같이 두었습니다. uv 를 안 쓰는 환경(CI, 다른 사람의 컴퓨터)에서 필요할 수 있어서요.
uv 로 설치하실 거면 `pyproject.toml` 만 보시면 됩니다.

둘을 함께 유지하실 거면 `uv add` 로 패키지를 바꾼 뒤 이렇게 다시 뽑으세요.

```powershell
uv export --no-hashes --format requirements-txt > requirements.txt
```
