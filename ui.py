# ui.py
import io as sm_ui_io
import qrcode as sm_ui_qrcode
import streamlit as sm_ui_st


def sm_ui_inject_custom_css():
    """Injects basic styling for a dark laptop dashboard."""
    sm_ui_st.markdown(
        """
        <style>
        .stApp { background-color: #0d1117; color: #c9d1d9; }
        .sm-ui-card { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def sm_ui_generate_qr_bytes(text: str) -> bytes:
    """Creates a QR code image from a text string."""
    qr = sm_ui_qrcode.QRCode(
        version=1,
        error_correction=sm_ui_qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = sm_ui_io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def sm_ui_render_tab_recovery(backend, run_case, demo_content: dict):
    """Draws the Recovery Control Center tab."""
    sm_ui_st.subheader("Autonomous Recovery Control Center")

    col_ctrl1, col_ctrl2 = sm_ui_st.columns([1, 1])
    with col_ctrl1:
        mode = sm_ui_st.radio(
            "Execution Mode",
            options=["live", "replay"],
            index=0,
            format_func=lambda x: "Live Gemini AI" if x == "live" else "Replay Demo (Backup)"
        )
    with col_ctrl2:
        if mode == "replay":
            sm_ui_st.warning("⚠️ Replay demo — no live AI")
        else:
            sm_ui_st.info("🤖 Live Gemini AI Active")

    sm_ui_st.divider()

    # Create Case Section
    col_create, col_select = sm_ui_st.columns([1, 1])
    with col_create:
        sm_ui_st.markdown("### Create New Demo Case")
        scenarios = demo_content.get("scenarios", [])
        scenario_options = {s["scenario_id"]: f"{s['title']} ({s['scenario_id']})" for s in scenarios}
        
        selected_scenario_id = sm_ui_st.selectbox(
            "Choose Scenario Seed", 
            options=list(scenario_options.keys()), 
            format_func=lambda x: scenario_options[x]
        )

        if sm_ui_st.button("Create Demo Case", use_container_width=True, type="primary"):
            new_case_id = backend.seed_scenario(selected_scenario_id)
            sm_ui_st.session_state.selected_case_id = new_case_id
            sm_ui_st.session_state.last_run_result = None
            sm_ui_st.rerun()

    # Select Existing Case Section
    with col_select:
        sm_ui_st.markdown("### Select Existing Case")
        all_cases = backend.list_cases()
        if all_cases:
            case_map = {c["case_id"]: f"{c['case_id']} - {c['title']} [{c['status']}]" for c in all_cases}
            case_ids = list(case_map.keys())
            
            current_idx = case_ids.index(sm_ui_st.session_state.selected_case_id) if sm_ui_st.session_state.selected_case_id in case_ids else 0
            
            chosen_case = sm_ui_st.selectbox("Select Case", options=case_ids, index=current_idx, format_func=lambda x: case_map[x])
            if chosen_case != sm_ui_st.session_state.selected_case_id:
                sm_ui_st.session_state.selected_case_id = chosen_case
                sm_ui_st.session_state.last_run_result = None
                sm_ui_st.rerun()
        else:
            sm_ui_st.info("No active cases.")

    active_case_id = sm_ui_st.session_state.selected_case_id

    # Display Active Case Details
    if active_case_id:
        sm_ui_st.divider()
        try:
            snapshot = backend.get_snapshot(active_case_id)
        except ValueError:
            snapshot = None

        if snapshot:
            sm_ui_st.markdown(f"### Active Case: `{active_case_id}`")
            st_col1, st_col2, st_col3, st_col4 = sm_ui_st.columns(4)
            st_col1.metric("Status", snapshot["status"])
            st_col2.metric("Payment Status", snapshot["payment_status"])
            st_col3.metric("Registrations", snapshot["registration_count"])
            st_col4.metric("Inbox Delivery", snapshot["delivery_status"])

            if sm_ui_st.button(f"▶ Run Agent Recovery ({mode.upper()})", use_container_width=True, type="primary"):
                with sm_ui_st.spinner("Agent executing..."):
                    def emit_callback(event):
                        backend.record_agent_event(active_case_id, event)
                    res = run_case(case_id=active_case_id, call_tool=backend.call_tool, emit=emit_callback, mode=mode, max_steps=8)
                    sm_ui_st.session_state.last_run_result = res
                    sm_ui_st.rerun()

            # Results & Logs
            last_res = sm_ui_st.session_state.last_run_result
            if last_res:
                if last_res.get("status") == "RESOLVED": sm_ui_st.success(f"**Success:** {last_res.get('summary')}")
                elif last_res.get("status") == "NEEDS_REVIEW": sm_ui_st.warning(f"**Escalated:** {last_res.get('summary')}")
                else: sm_ui_st.error(f"**Error:** {last_res.get('summary')}")

            receipt = snapshot.get("receipt") or (last_res.get("receipt") if last_res else None)
            if receipt:
                with sm_ui_st.expander("📜 Verified Receipt", expanded=True):
                    sm_ui_st.json(receipt)

            sm_ui_st.markdown("### Activity Log")
            for ev in reversed(backend.get_audit(active_case_id) or []):
                ok_str = "✅" if ev.get("ok") is True else ("❌" if ev.get("ok") is False else "ℹ️")
                sm_ui_st.markdown(f"**{ev.get('timestamp', '')}** | {ok_str} `{ev.get('tool') or 'AI'}` {ev.get('summary', '')}")


def sm_ui_render_tab_inbox(backend):
    """Draws the Local Demo Inbox tab."""
    sm_ui_st.subheader("Local Demo Inbox")
    active_case_id = sm_ui_st.session_state.selected_case_id
    
    if not active_case_id:
        sm_ui_st.info("Select a case in the Recovery tab first.")
        return

    try:
        snapshot = backend.get_snapshot(active_case_id)
    except Exception:
        snapshot = None

    if snapshot and snapshot.get("delivery_status") == "delivered" and snapshot.get("ticket_token"):
        sm_ui_st.success(f"📩 1 New Message for Case `{active_case_id}`")
        col_msg, col_qr = sm_ui_st.columns([2, 1])
        
        with col_msg:
            sm_ui_st.markdown(f"**Ticket ID:** `{snapshot['ticket_id']}`\n\n**Token:** `{snapshot['ticket_token']}`")
        with col_qr:
            sm_ui_st.image(sm_ui_generate_qr_bytes(snapshot["ticket_token"]), caption="Ticket QR Code", width=180)
    else:
        sm_ui_st.warning("No delivered messages found for this case.")


def sm_ui_render_tab_checker(backend):
    """Draws the Ticket Verification Checker tab."""
    sm_ui_st.subheader("Ticket Verification Checker")
    input_token = sm_ui_st.text_input("Paste Ticket Token", placeholder="e.g. TKT-TOKEN-12345")

    if sm_ui_st.button("Verify Token", type="primary"):
        if input_token.strip():
            res = backend.verify_token(input_token.strip())
            if res.get("valid"):
                sm_ui_st.success("✅ Ticket Token is VALID!")
                sm_ui_st.json(res)
            else:
                sm_ui_st.error("❌ Invalid Ticket Token.")
        else:
            sm_ui_st.warning("Please enter a token.")


def sm_ui_render_tab_about(demo_content: dict):
    """Draws the About tab."""
    sm_ui_st.subheader(f"About {demo_content.get('product_name', 'SentinelMesh')}")
    if demo_content.get("pitch_summary"): sm_ui_st.markdown(f"**Pitch:** {demo_content['pitch_summary']}")
    if demo_content.get("scope_note"): sm_ui_st.info(f"📌 {demo_content['scope_note']}")


def sm_render_app(backend, run_case, demo_content: dict) -> None:
    """The main public function that Het's backend calls to launch the UI."""
    sm_ui_inject_custom_css()
    sm_ui_st.title(f"⚡ {demo_content.get('product_name', 'SentinelMesh')}")

    # Initialize Session State
    if "selected_case_id" not in sm_ui_st.session_state:
        sm_ui_st.session_state.selected_case_id = None
    if "last_run_result" not in sm_ui_st.session_state:
        sm_ui_st.session_state.last_run_result = None

    # Create the 4 Tabs
    tab_recovery, tab_inbox, tab_checker, tab_about = sm_ui_st.tabs(
        ["🔄 Recovery", "📥 Demo Inbox", "🎟️ Ticket Checker", "ℹ️ About"]
    )

    # Run the simple functions inside each tab
    with tab_recovery:
        sm_ui_render_tab_recovery(backend, run_case, demo_content)
    with tab_inbox:
        sm_ui_render_tab_inbox(backend)
    with tab_checker:
        sm_ui_render_tab_checker(backend)
    with tab_about:
        sm_ui_render_tab_about(demo_content)
