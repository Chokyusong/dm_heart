# dm_ui.py
# -*- coding: utf-8 -*-

import io, os, re, json, time, sys, subprocess
from pathlib import Path
from typing import Tuple, List

import pandas as pd
import streamlit as st

# (선택) 자동 새로고침
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None


# =========================
# 경로/파일 상수
# =========================
BASE_DIR = Path(__file__).parent
RECIP_CSV = BASE_DIR / "recipients_preview.csv"
MESSAGE_TXT = BASE_DIR / "message.txt"
ENV_FILE = BASE_DIR / ".env"
STATUS_JSON = BASE_DIR / "send_status.json"
SENDER_PY = BASE_DIR / "panda_dm_sender.py"   # 외부 전송 스크립트
# 경로/파일 상수 근처에 추가
LOG_OUT = BASE_DIR / "sender_stdout.log"
LOG_ERR = BASE_DIR / "sender_stderr.log"


# =========================
# 공통 유틸
# =========================
FULLWIDTH_SPACE = "\u3000"  # 전각 공백(U+3000)

def now_ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_status(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": [], "meta": {}}

def save_status(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# CSV 컬럼 추론/정리
# =========================
def guess_columns(df: pd.DataFrame) -> Tuple[str, str, str]:
    cols = [str(c).strip() for c in df.columns]
    id_cands    = ["후원아이디", "아이디", "ID", "id", "userId", "후원 아이디", "후원 아이디(닉네임)"]
    nick_cands  = ["닉네임", "후원닉네임", "닉", "별명", "name", "nick"]
    heart_cands = ["후원하트", "하트", "hearts", "heart", "총하트", "하트수"]

    def pick(cands):
        for c in cols:
            if c.replace(" ", "") in [x.replace(" ", "") for x in cands]:
                return c
        return ""

    # 기본값은 "아이디 / 닉네임 / 후원하트" 순서 느낌으로 지정
    id_col = pick(id_cands) or cols[0]
    nick_col = pick(nick_cands) or ""
    heart_col = pick(heart_cands) or cols[-1]
    return id_col, nick_col, heart_col

def normalize_id_from_mix(x: str) -> str:
    """'aa123(닉네임)'에서 ID만 추출"""
    if pd.isna(x): return ""
    s = str(x).strip()
    m = re.match(r"^\s*([^()]+)", s)
    return (m.group(1) if m else s).strip()

def normalize_nick_from_mix(x: str) -> str:
    """'aa123(닉네임)'에서 닉네임만 추출"""
    if pd.isna(x): return ""
    s = str(x).strip()
    m = re.search(r"\((.*?)\)", s)
    return (m.group(1).strip() if m else "")

def detect_mixed_id(series: pd.Series, sample: int = 200, threshold: float = 0.3) -> bool:
    """값 중 '( )' 패턴이 일정 비율 이상이면 혼합 컬럼으로 간주"""
    try:
        vals = series.dropna().astype(str).head(sample)
        hit = sum(("(" in v and ")" in v and v.find("(") < v.find(")")) for v in vals)
        return (len(vals) > 0) and (hit / len(vals) >= threshold)
    except Exception:
        return False


def prepare_from_csv(
    df: pd.DataFrame,
    id_col: str,
    nick_col: str,
    heart_col: str,
    force_mixed: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    - '아이디(닉네임)' 혼합 여부를 자동 감지(+토글)해서 분리
    - 닉네임은 혼합에서 추출값 우선, 없으면 별도 닉네임 컬럼 사용
    - 같은 ID 합산(=당일 총합) 후 자동발송/ VIP 분리
    """
    tmp = df.copy()
    tmp.columns = [str(c).strip() for c in tmp.columns]

    def _to_int(x):
        s = str(x).strip().replace(",", "")
        try:
            return int(float(s))
        except:
            return 0

    series_id = tmp[id_col]
    mixed = force_mixed or detect_mixed_id(series_id)

    if mixed:
        tmp["후원아이디"] = series_id.map(normalize_id_from_mix)
        tmp["닉네임_from_mix"] = series_id.map(normalize_nick_from_mix)
    else:
        tmp["후원아이디"] = series_id.astype(str).str.strip()
        tmp["닉네임_from_mix"] = ""

    if nick_col:
        tmp["닉네임_src"] = tmp[nick_col].astype(str).str.strip()
    else:
        tmp["닉네임_src"] = ""

    tmp["닉네임"] = tmp["닉네임_from_mix"]
    mask_empty = tmp["닉네임"].isna() | (tmp["닉네임"].astype(str).str.len() == 0)
    tmp.loc[mask_empty, "닉네임"] = tmp.loc[mask_empty, "닉네임_src"]

    tmp["후원하트"] = tmp[heart_col].apply(_to_int)

    # 같은 ID 총합(=하루 합계)
    agg = (
        tmp.groupby(["후원아이디"], as_index=False)
           .agg(닉네임=("닉네임", "first"), 후원하트=("후원하트", "sum"))
    )

    auto_df = agg[(agg["후원하트"] >= 1000) & (agg["후원하트"] < 10000)].copy()
    vip_df  = agg[agg["후원하트"] >= 10000].copy()

    auto_df = auto_df.sort_values(["후원하트", "후원아이디"], ascending=[False, True]).reset_index(drop=True)
    vip_df  = vip_df.sort_values(["후원하트", "후원아이디"],  ascending=[False, True]).reset_index(drop=True)
    return auto_df, vip_df


# =========================
# 메시지 변형(5명마다 줄 끝 공백)
# =========================
def build_messages_with_endspaces(base_msg: str, n: int) -> List[str]:
    """
    5명마다 대상 줄의 '끝'에 전각 공백(U+3000)을 추가.
      g = i // 5
      add_line_idx = g % L
      add_spaces   = g // L + 1
    """
    lines = base_msg.splitlines() or [base_msg]
    L = max(1, len(lines))
    out: List[str] = []

    for i in range(n):
        g = i // 5
        add_line_idx = g % L
        add_spaces = (g // L) + 1

        mutated = []
        for j, ln in enumerate(lines):
            if j == add_line_idx:
                mutated.append(ln + (FULLWIDTH_SPACE * add_spaces))
            else:
                mutated.append(ln)
        msg = "\n".join(mutated)
        out.append(msg[:500] if len(msg) > 500 else msg)
    return out



# =========================
# 파일 저장(.env/CSV/MSG)
# =========================
def save_local_bundle(out_df: pd.DataFrame, base_message: str, panda_id: str, panda_pw: str):
    RECIP_CSV.write_text(out_df.to_csv(index=False), encoding="utf-8")
    MESSAGE_TXT.write_text(base_message, encoding="utf-8")
    if panda_id and panda_pw:
        ENV_FILE.write_text(f"PANDA_ID={panda_id}\nPANDA_PW={panda_pw}\n", encoding="utf-8")


# =========================
# 전송 실행(실시간 로그/대시보드)
# =========================
def run_sender_realtime(headless: bool, start: int, limit: int, reset_status: bool):
    """전송 프로세스를 백그라운드로 시작만 하고, 화면은 1초마다 자동 새로고침되며
    로그 파일과 send_status.json을 읽어 렌더링한다."""
    if not SENDER_PY.exists():
        st.error(f"전송 스크립트를 찾을 수 없습니다: {SENDER_PY}")
        return

    if not RECIP_CSV.exists():
        st.error("recipients_preview.csv가 없습니다. 먼저 대상을 만들어 저장하세요.")
        return

    if not MESSAGE_TXT.exists():
        st.error("message.txt가 없습니다. 먼저 메시지를 저장하세요.")
        return

    # 이미 실행 중이면 중복 실행 방지
    if st.session_state.get("sender_running"):
        st.info("이미 전송이 진행 중입니다. 아래 로그/대시보드를 확인하세요.")
        return

    cmd = [sys.executable, str(SENDER_PY)]
    if headless:
        cmd.append("--headless")
    if reset_status:
        cmd.append("--reset")
    cmd += ["--status-file", str(STATUS_JSON)]
    if start and int(start) > 0:
        cmd += ["--start", str(int(start))]
    if limit and int(limit) > 0:
        cmd += ["--limit", str(int(limit))]

    # 로그 파일 초기화
    try:
        LOG_OUT.write_text("", encoding="utf-8")
        LOG_ERR.write_text("", encoding="utf-8")
    except Exception:
        pass

    # 백그라운드 실행 (출력 파일로 리다이렉트)
    out_f = open(LOG_OUT, "a", encoding="utf-8", buffering=1)
    err_f = open(LOG_ERR, "a", encoding="utf-8", buffering=1)

    proc = subprocess.Popen(
        cmd,
        stdout=out_f,
        stderr=err_f,
        text=True,
        bufsize=1,
    )

    st.session_state["sender_running"] = True
    st.session_state["sender_pid"] = proc.pid
    st.success("전송을 시작했습니다. 로그/대시보드는 1초마다 자동 새로고침됩니다.")


def render_dashboard():
    st.subheader("📊 실시간 대시보드")
    if STATUS_JSON.exists():
        try:
            data = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except Exception:
            data = {"items": [], "meta": {}}
        items = data.get("items", [])
        if not items:
            st.info("현황 파일은 있으나 항목이 없습니다. (전송 시작 후 생성)")
            return
        df = pd.DataFrame(items)

        def lamp(s):
            return "🟢 성공" if s == "success" else ("🟡 실패" if s == "fail" else "🔴 대기")
        df["상태등"] = df["status"].map(lamp)

        c1, c2, c3, c4 = st.columns(4)
        total = len(df)
        succ = int((df["status"] == "success").sum())
        fail = int((df["status"] == "fail").sum())
        pend = total - succ - fail
        c1.metric("총 대상", total)
        c2.metric("성공", succ)
        c3.metric("실패", fail)
        c4.metric("대기", pend)

        st.dataframe(
            df[["index", "id", "status", "상태등", "updated"]]
              .rename(columns={"index": "순번", "id": "후원아이디", "updated": "최근시각"}),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "🖫 전송 현황(JSON) 다운로드",
            data=STATUS_JSON.read_bytes(),
            file_name="send_status.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.info("send_status.json 파일이 아직 없습니다. 전송을 시작하면 생성됩니다.")


# =========================
# 메인 UI (쪽지 발송 탭)
# =========================
def show():
    
    # 실행 중이거나 현황 파일이 있으면 1초마다 자동 새로고침
    try:
        is_running = st.session_state.get("sender_running", False)
        if is_running or STATUS_JSON.exists():
            if st_autorefresh:
                st_autorefresh(interval=1000, key="dm_autorefresh_dashboard", limit=None)
    except Exception:
        pass

    st.subheader("✉️ PandaLive 쪽지 발송")

    # 좌: 자격/메시지, 우: CSV/수동
    cA, cB = st.columns([1, 2])

    # ── 좌측: .env / 메시지
    with cA:
        st.markdown("#### 팬더 계정(.env 저장)")
        panda_id = st.text_input("팬더 아이디", value="", placeholder="예: theh1359")
        panda_pw = st.text_input("팬더 비밀번호", value="", type="password")

        st.markdown("#### 기본 쪽지 내용 (여러 줄)")
        base_message = st.text_area(
            "메시지",
            value="",
            height=220,
            placeholder="첫 줄\n둘째 줄\n셋째 줄 …",
            help="5명마다 어느 한 줄의 끝에 공백을 자동 추가(중복 전송 제한 회피).",
        )

    # ── 우측: CSV 업로드 + 수동 ID
    with cB:
        st.markdown("#### 원본 CSV 업로드")
        up = st.file_uploader("CSV를 업로드하세요", type=["csv"])

        st.markdown("#### 또는: 수동 ID 입력 (테스트용)")
        manual_ids = st.text_area(
            "ID 목록(줄바꿈/쉼표/공백 구분)", height=110, placeholder="id1\nid2\nid3"
        )

    auto_df = pd.DataFrame(columns=["후원아이디", "닉네임", "후원하트"])
    vip_df  = pd.DataFrame(columns=["후원아이디", "닉네임", "후원하트"])

    # ── CSV 처리
    if up is not None:
        try:
            raw = pd.read_csv(up)
        except Exception:
            up.seek(0)
            raw = pd.read_csv(up, encoding="utf-8-sig")

        st.markdown("---")
        st.markdown("##### 1) 컬럼 매핑")

        id_guess, nick_guess, heart_guess = guess_columns(raw)
        cols = list(raw.columns)

        # 안전한 기본 index
        def idx_of(name, default=0):
            try:
                return cols.index(name)
            except Exception:
                return default

        c1, c2, c3 = st.columns(3)
        with c1:
            id_col = st.selectbox("후원아이디 컬럼", options=cols, index=idx_of(id_guess, 0))
        with c2:
            nick_candidates = ["(없음)"] + cols
            nick_index = 0 if not nick_guess else (cols.index(nick_guess) + 1)
            nick_col = st.selectbox("닉네임 컬럼(없으면 '(없음)')", options=nick_candidates, index=nick_index)
            nick_col = "" if nick_col == "(없음)" else nick_col
        with c3:
            heart_col = st.selectbox("후원하트 컬럼", options=cols, index=idx_of(heart_guess, len(cols)-1))

        # 혼합 자동감지 + 토글
        auto_mixed_guess = detect_mixed_id(raw[id_col])
        force_mixed = st.checkbox(
            "선택한 아이디 컬럼이 '아이디(닉네임)' 혼합 형식입니다",
            value=auto_mixed_guess,
            help="예: aa123(닉네임). 자동 감지 결과를 기본값으로 표시합니다.",
        )

        auto_df, vip_df = prepare_from_csv(raw, id_col, nick_col, heart_col, force_mixed=force_mixed)

        # 미리보기
        st.markdown("##### 2) 추출 결과 미리보기")
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("**🎯 자동 발송 대상 (1,000 ~ 9,999 하트)**")
            st.caption(f"총 대상자: **{len(auto_df)}명** — 같은 ID는 합산 기준")
            st.dataframe(auto_df, use_container_width=True, hide_index=True)
        with right:
            st.markdown("**👑 VIP (10,000+ 하트) — 수동 발송**")
            st.caption(f"총 VIP: **{len(vip_df)}명**")
            st.dataframe(vip_df, use_container_width=True, hide_index=True)

        # 변형 메시지 미리보기(자동발송 대상 기준)
        st.markdown("##### 3) 변형 메시지 미리보기 (자동발송 대상)")
        msgs = build_messages_with_endspaces(base_message, len(auto_df))
        preview = pd.DataFrame(
            {
                "순번": list(range(len(auto_df))),
                "후원아이디": auto_df["후원아이디"],
                "닉네임": auto_df["닉네임"],
                "후원하트": auto_df["후원하트"],
                "메시지": msgs,
            }
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)

        # 저장
        if st.button("💾 파일 저장 (recipients_preview.csv / message.txt / .env)"):
            save_local_bundle(auto_df, base_message, panda_id, panda_pw)
            st.success("저장 완료!")

        # VIP CSV 다운로드
        vip_csv = vip_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("🖫 VIP 목록 CSV 다운로드", data=vip_csv, file_name="vip_list.csv",
                           mime="text/csv", use_container_width=True)

    # ── 수동 ID만으로 생성
    if (up is None) and manual_ids.strip():
        tokens = [t.strip() for t in re.split(r"[,\s]+", manual_ids) if t.strip()]
        tokens = list(dict.fromkeys(tokens))  # dedup, keep order
        auto_df = pd.DataFrame({"후원아이디": tokens, "닉네임": ["" for _ in tokens], "후원하트": [1000 for _ in tokens]})
        st.info(f"수동 대상자: {len(auto_df)}명")
        st.dataframe(auto_df, use_container_width=True, hide_index=True)

        msgs = build_messages_with_endspaces(base_message, len(auto_df))
        preview = pd.DataFrame({"순번": list(range(len(auto_df))), "후원아이디": auto_df["후원아이디"], "메시지": msgs})
        st.markdown("##### 변형 메시지 미리보기")
        st.dataframe(preview, use_container_width=True, hide_index=True)

        if st.button("💾 파일 저장 (recipients_preview.csv / message.txt / .env)", key="save-manual"):
            save_local_bundle(auto_df, base_message, panda_id, panda_pw)
            st.success("저장 완료!")

    # ── 실행/현황
    st.markdown("---")
    st.markdown("### 🚀 전송 실행")

    col1, col2, col3, col4 = st.columns([1, 1, 1, 2])
    with col1:
        start_idx = st.number_input("시작 인덱스", min_value=0, value=0, step=1)
    with col2:
        limit_cnt = st.number_input("최대 인원(0=전원)", min_value=0, value=0, step=1)
    with col3:
        headless = st.checkbox("헤드리스 실행", value=True)
        reset_status = st.checkbox("현황 초기화", value=False)
    with col4:
        if st.button("📨 전송 실행", use_container_width=True):
            # 현황 초기화 옵션일 때 파일 제거
            if reset_status:
                try:
                    STATUS_JSON.unlink(missing_ok=True)
                except Exception:
                    pass

            # 전송 전 seed 현황(대기 상태) 생성 → 대시보드가 즉시 보임
            if RECIP_CSV.exists():
                try:
                    df_seed = pd.read_csv(RECIP_CSV)
                    st_json = {
                        "items": [
                            {"index": int(i), "id": str(r["후원아이디"]),
                             "status": "pending", "updated": now_ts()}
                            for i, r in df_seed.iterrows()
                        ],
                        "meta": {"created": now_ts()},
                    }
                    save_status(STATUS_JSON, st_json)
                except Exception:
                    pass

            run_sender_realtime(headless=headless, start=start_idx, limit=limit_cnt, reset_status=reset_status)

    st.markdown("---")
    render_dashboard()
    st.markdown("### ⏹ 실행 제어")
    if st.button("강제 종료"):
        pid = st.session_state.get("sender_pid")
        if pid:
            try:
                # Windows 호환 강제 종료
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"])
                else:
                    os.kill(pid, 9)
                st.success("프로세스를 강제 종료했습니다.")
            except Exception as e:
                st.error(f"종료 실패: {e}")
        st.session_state["sender_running"] = False

    st.markdown("#### 🧹 현황/임시 파일 관리")
    st.markdown("---")
    st.subheader("📝 실시간 로그")
    # 표준 출력 로그
    if LOG_OUT.exists():
        try:
            out_txt = LOG_OUT.read_text(encoding="utf-8")[-10000:]  # 너무 길면 마지막 10KB만
            st.expander("STDOUT", expanded=True).code(out_txt or "(로그 없음)")
        except Exception:
            st.info("STDOUT 로그를 읽을 수 없습니다.")

    # 표준 에러 로그
    if LOG_ERR.exists():
        try:
            err_txt = LOG_ERR.read_text(encoding="utf-8")[-10000:]
            st.expander("STDERR", expanded=False).code(err_txt or "(에러 로그 없음)")
        except Exception:
            st.info("STDERR 로그를 읽을 수 없습니다.")

    if st.button("현황/임시 파일 삭제", help="send_status.json / recipients_preview.csv / message.txt / .env 제거"):
        for p in [STATUS_JSON, RECIP_CSV, MESSAGE_TXT, ENV_FILE]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        st.success("삭제 완료!")
