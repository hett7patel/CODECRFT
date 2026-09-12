SM_DEMO_CONTENT = {
    "product_name": "SentinelMesh",
    "tagline": "Autonomous workflow recovery and synchronization.",
    "scope_note": "Business APIs, payments, and inbox delivery in this demo are local simulations backed by real local database changes, not live third-party production systems.",
    "pitch_summary": "SentinelMesh is an agentic workflow controller that observes broken business processes and repairs them autonomously. Instead of rigid if/then rules, our agent inspects actual database states—like a confirmed payment missing an event ticket—and dynamically executes the right tools to resolve the gap. We demonstrate this using local simulations for payments and email, proving how AI can handle uncertain states and unexpected network failures to ensure data consistency without human intervention.",
    "scenarios": [
        {
            "scenario_id": "missing_registration",
            "title": "Missing Registration",
            "description": "The user has a confirmed payment, but the initial registration step failed. No ticket or local delivery exists yet.",
            "payment_status": "confirmed",
            "registration_exists": False,
            "ticket_exists": False,
            "delivery_exists": False,
            "lose_ticket_response_once": False
        },
        {
            "scenario_id": "delivery_failed",
            "title": "Delivery Failed",
            "description": "Registration and ticket generation succeeded for this confirmed payment, but the final local inbox delivery step failed.",
            "payment_status": "confirmed",
            "registration_exists": True,
            "ticket_exists": True,
            "delivery_exists": False,
            "lose_ticket_response_once": False
        },
        {
            "scenario_id": "lost_response",
            "title": "Ambiguous Ticket Response",
            "description": "Registration is complete. The system will simulate a network timeout during ticket creation. The ticket will actually be created, but the agent must inspect the database to realize this before proceeding.",
            "payment_status": "confirmed",
            "registration_exists": True,
            "ticket_exists": False,
            "delivery_exists": False,
            "lose_ticket_response_once": True
        },
        {
            "scenario_id": "payment_unconfirmed",
            "title": "Unconfirmed Payment",
            "description": "The payment is still pending. The system must not create any registration or ticket, and the case should be escalated for human review.",
            "payment_status": "pending",
            "registration_exists": False,
            "ticket_exists": False,
            "delivery_exists": False,
            "lose_ticket_response_once": False
        }
    ]
}