def get_main_menu_keyboard():
    return [
        [("🛒 Buy Subscription", "buy")],
        [("👤 My Profile / Subscriptions", "profile")],
        [("ℹ️ Help / Support", "help")],
    ]


def get_packages_keyboard(packages: list[dict]):
    return [
        [(pkg["label"], f"pkg_{pkg['id']}")]
        for pkg in packages
    ]


def get_durations_keyboard(package_id: int, durations: list[dict]):
    return [
        [(d["label"], f"dur_{package_id}_{d['id']}")]
        for d in durations
    ] + [[("🔙 Back to Packages", "buy")]]


def get_payment_keyboard(order_id: int):
    return [
        [("✅ I have paid", f"paid_{order_id}")],
        [("❌ Cancel", f"cancel_{order_id}")],
    ]


def get_back_to_menu_keyboard():
    return [[("🔙 Main Menu", "menu")]]
