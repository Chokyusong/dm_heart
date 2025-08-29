# main_app.py
import streamlit as st

st.set_page_config(page_title="하트 합계 & 쪽지 발송", layout="wide")

#==== 비밀번호 잠금 ====
password = st.text_input("비밀번호를 입력하세요", type="password")

if password != "abwmdpsxj!234":  # 👉 원하는 비밀번호로 변경
    st.error("접속 권한이 없습니다.")
    st.stop()
    
st.title("하트 합계 & 쪽지 발송")


# ── 두 탭만! (합계 / 쪽지) ────────────────────────────────────────
tab1, tab2 = st.tabs(["📊 하트 합계", "✉️ 쪽지 발송"])

with tab1:
    from heart_aggregate import show as show_aggregate  # 기존 app.py 래핑본
    show_aggregate()

with tab2:
    from dm_ui import show as show_dm  # 기존 panda_dm_app.py 래핑본
    show_dm()
