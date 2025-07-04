import sys
import pandas as pd
from rapidfuzz import process, fuzz
from openai import OpenAI
import requests
import json
import re
from math import radians, sin, cos, sqrt, atan2
from PyQt5.QtCore import QThread, pyqtSignal, QObject

import logging

logging.basicConfig(
    level=logging.DEBUG,                           # כל הודעות DEBUG ומעלה יודפסו
    format="%(asctime)s %(levelname)-8s %(message)s"  # איך פורמט ההודעה תיראה
)
logger = logging.getLogger(__name__)  # יוצרים לוגגר בשם המודול


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLineEdit, QPushButton, QLabel, QFrame, QSizePolicy,
    QGraphicsDropShadowEffect
)
from PyQt5.QtCore import Qt, QSize, QTimer
from PyQt5.QtGui import QFont, QColor

# === הגדרות API keys ===
OPENAI_API_KEY = " "
GOOGLE_API_KEY = " "

# אתחול OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# === טעינת נתוני Excel ופענוח עמודות ===
df = pd.read_excel("סינון.xlsx")
df.columns = df.columns.str.strip()

product_col = next((c for c in df.columns if 'מוצר' in c), None)
price_col = next((c for c in df.columns if 'מחיר' in c), None)
if not product_col or not price_col:
    raise KeyError("יש לוודא שקיימות עמודות מוצרים ו-מחיר בקובץ.")

# המרת מחיר ממחרוזת ל-float (הסרת '₪', החלפת ',' לנקודה, הסרת רווחים)
df[price_col] = (
    df[price_col]
    .astype(str)
    .str.replace('₪', '', regex=False)
    .str.replace('\xa0', '', regex=False)
    .str.replace(',', '.')
    .str.strip()
    .astype(float)
)

# אימוג'ים לפי מוצר (מילות מפתח)
EMOJIS = {
    'חלב': '🥛', 'ביצים': '🥚', 'עגבנייה': '🍅', 'מלפפון': '🥒',
    'לחם': '🍞', 'סוכר': '🍚', 'חמאה': '🧈', 'יוגורט': '🍶',
    'מנגו': '🥭'
}

known_products = df[product_col].unique().tolist()
all_chains = df['סופרמרקט'].unique().tolist()

# === מילים־טריגר שמתחילות את תהליך איסוף המוצרים ===
TRIGGERS = [
]


conversation_state = {
    "stage": "chat",  # chat / ask_location / ask_cart / wait_for_selection
    "location": "",
    "cart_items": [],
    "pending_choices": {},
    "pending_order": [],
    "pending_index": 0,
    "history": []
}


# === Geocoding + Places API ===

def geocode_address(address: str):
    """
    מחזיר (lat, lng) של הכתובת שהמשתמש הזין, או None אם לא נמצא.
    """
    url = (
        "https://maps.googleapis.com/maps/api/geocode/json"
        f"?address={requests.utils.quote(address)}"
        f"&key={GOOGLE_API_KEY}"
    )
    resp = requests.get(url).json()
    if resp.get("results"):
        loc = resp["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None


def get_closest_branch(chain_name: str, user_address: str):
    """
    מוצא את הסניף הקרוב ביותר של chain_name סמוך לכתובת user_address.
    משתמש בשלבים:
     1. גיאוקודינג כדי לקבל (lat, lng) של הכתובת.
     2. Places Nearby Search עם פרמטר name=<chain_name> (type=supermarket, rankby=distance).
     3. מסנן ביד קשה רק תוצאות שבהן place["name"] מכיל chain_name.
     4. לוקח את הסניף הראשון (אם קיים), ומחזיר (display_text, maps_url).
       אחרת – מחזיר (None, None).
    """
    # 1. גיאוקודינג
    user_loc = geocode_address(user_address)
    if not user_loc:
        return None, None
    user_lat, user_lng = user_loc

    # 2. קריאה ל-Places Nearby Search עם פרמטר name=<chain_name>
    places_url = (
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        f"?location={user_lat},{user_lng}"
        f"&rankby=distance"
        f"&name={requests.utils.quote(chain_name)}"
        f"&type=supermarket"
        f"&key={GOOGLE_API_KEY}"
    )
    resp = requests.get(places_url).json()
    results = resp.get("results", [])

    # אם אין תוצאות כלל, מחזירים None
    if not results:
        return None, None

    # 3. מסננים (hard-filter) רק את הסניפים שבהם 'name' מכיל בדיוק chain_name
    filtered = [r for r in results if chain_name in r.get("name", "")]
    if not filtered:
        return None, None

    # 4. לוקחים את הסניף הראשון (הקרוב ביותר מבין המסוננים)
    first = filtered[0]
    name = first.get("name", "")
    vicinity = first.get("vicinity") or first.get("formatted_address", "")

    # קואורדינטות של הסניף כדי לבנות URL ניווט
    loc2 = first.get("geometry", {}).get("location", {})
    branch_lat = loc2.get("lat")
    branch_lng = loc2.get("lng")

    maps_url = None
    if branch_lat is not None and branch_lng is not None:
        maps_url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&destination={branch_lat},{branch_lng}"
        )

    display_text = f"{name} — {vicinity}"
    return display_text, maps_url


# === חיפוש מוצרים והצגת מחירים ===

def find_product_match(q: str, strict_thresh: int = 95, loose_thresh: int = 85) -> list[str]:
    """
    קודם מנסים התאמות באמצעות token_set_ratio (ב־strict_thresh).
    אם אין תוצאות – מנסים partial_ratio (ב־loose_thresh).
    """
    results_strict = process.extract(
        query=q,
        choices=known_products,
        scorer=fuzz.token_set_ratio,
        limit=5
    )
    matches = [m for m, score, _ in results_strict if score >= strict_thresh]
    if matches:
        return matches

    results_loose = process.extract(
        query=q,
        choices=known_products,
        scorer=fuzz.partial_ratio,
        limit=5
    )
    return [m for m, score, _ in results_loose if score >= loose_thresh]

SYSTEM_PROMPT = (
    "אתה צ'אטבוט שמח, נחמד ומנומס. "
    "ענה רק על נושאים הקשורים לקניות, מתכונים, לסופרים, למוצרים וערכים תזונתיים. "
    "אם המשתמש שואל על דבר שאינו קשור, ענה בנימוס: "
    "״סליחה, אני מתמקד רק בנושאי קניות ומוצרים. איך אוכל לסייע בתחום הזה?״"
)


def ask_openai(text: str) -> str:
    """
    שולח בקשה ל-OpenAI ChatGPT עם הגדרת מערכת ומשתמש ב-history.
    """
    try:
        # 1) SYSTEM PROMPT
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 2) צורף את ה-history (עד 20 הודעות אחרונות בלבד)
        msgs += conversation_state["history"][-20:]
        msgs.append({"role": "user", "content": text})
        logger.debug("ask_openai() called • prompt=%r • history_len=%d", text, len(conversation_state["history"]))


        # 3) שליחת הבקשה
        resp = client.chat.completions.create(
            model="gpt-4",
            temperature=0.3,
            max_tokens=500,
            messages = msgs
        )

        # 4) אחזור ושמור את התשובה
        answer = resp.choices[0].message.content
        logger.debug("ask_openai() response received • answer=%r", answer)
        conversation_state["history"].append({"role": "assistant", "content": answer})
        return answer

    except Exception as e:
        return f"שגיאה ב־OpenAI: {e}"



def classify_user_input(text: str) -> dict:
    """
    מחזירה dict עם:
       - intent: 'add_items' / 'compute_cart' / 'general'
       - items: []  – רשימת טוקנים שהסריקה זיהתה כמוצרים
    """
    prompt = (
        "You are an intent-and-entity extractor.\n"
        "Input: מה אומר המשתמש?\n"
        "Output JSON with two fields:\n"
        "  intent: one of [add_items, compute_cart, general]\n"
        "  items: list of words that look like grocery items\n"
        "Examples:\n"
        "User: 'אני צריך חלב ביצים ולחם'\n"
        "=> {\"intent\":\"add_items\",\"items\":[\"חלב\",\"ביצים\",\"לחם\"]}\n"
        "User: 'שלח לי רשימת קניות'\n"
        "=> {\"intent\":\"compute_cart\",\"items\":[]}\n"
        "User: 'מה מזג האוויר היום?'\n"
        "=> {\"intent\":\"general\",\"items\":[]}\n"
        f"Now classify:\nUser: '{text}'\n=>"
    )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"system","content":prompt}],
        temperature=0
    )
    return json.loads(resp.choices[0].message.content)

def calculate_totals(items: list[str]) -> dict[str, float]:
    """
    מחשב עלות כוללת לכל רשת, בהתבסס על רשימת המוצרים שבחרנו.
    """
    totals = {c: 0.0 for c in all_chains}
    for prod in items:
        rows = df[df[product_col] == prod]
        for c in all_chains:
            pr = rows[rows['סופרמרקט'] == c][price_col]
            if not pr.empty:
                totals[c] += pr.iloc[0]
    return totals


# === ממשק המשתמש עם PyQt5 ===

class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🤖🛒 SmartCart – סוכן הקניות החכם")
        self.setMinimumSize(QSize(900, 700))
        self.setLayoutDirection(Qt.RightToLeft)


        # רקע כללי
        self.setStyleSheet("QMainWindow { background-color: #FAFAFA; }")

        # וידג'ט מרכזי
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self.setCentralWidget(central)

        # כותרת עליונה (Header)
        header = QLabel("🤖🛒 SmartCart – סוכן הקניות החכם")
        header.setFont(QFont("Segoe UI", 18, QFont.Bold))
        header.setStyleSheet(
            "QLabel { background-color: #075E54; color: #FFFFFF; padding: 12px; }"
        )
        header.setAlignment(Qt.AlignCenter)
        central_layout.addWidget(header)

        # אזור שיחה (QScrollArea)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area_widget = QWidget()

        self.scroll_layout = QVBoxLayout(self.scroll_area_widget)
        self.scroll_layout.setContentsMargins(10, 10, 10, 10)
        self.scroll_layout.setSpacing(10)
        self.scroll_layout.setAlignment(Qt.AlignTop)

        # מרווח גמיש בתחתית (כדי שהבועות יתמקמו למעלה)
        self.scroll_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_area_widget)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: #ECE5DD; }")
        central_layout.addWidget(self.scroll_area, 1)

        # מסגרת קלט עם QLineEdit וכפתור "שלח"
        input_frame = QWidget()
        input_frame.setStyleSheet(
            "QWidget { background-color: #FFFFFF; border-top: 1px solid #CCCCCC; padding: 8px; }"
        )
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(10, 0, 10, 0)
        input_layout.setSpacing(8)

        self.input_entry = QLineEdit()
        self.input_entry.setFont(QFont("Segoe UI", 12))
        self.input_entry.setPlaceholderText("הקלד כאן...")
        self.input_entry.setLayoutDirection(Qt.RightToLeft)
        self.input_entry.setStyleSheet(
            "QLineEdit { border: 1px solid #CCCCCC; border-radius: 18px; padding: 6px 10px; background-color: #FFFFFF; }"
            "QLineEdit:focus { border: 1px solid #888888; }"
        )
        self.input_entry.returnPressed.connect(self.on_send)
        input_layout.addWidget(self.input_entry, 1)

        self.send_button = QPushButton("שלח")
        self.send_button.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.send_button.setFixedSize(60, 36)
        self.send_button.setStyleSheet(
            "QPushButton { background-color: #25D366; color: white; border: none; border-radius: 18px; }"
            "QPushButton:hover { background-color: #1EBE54; }"
        )
        self.send_button.clicked.connect(self.on_send)
        input_layout.addWidget(self.send_button)

        central_layout.addWidget(input_frame)

        # משתנים לעיבוד אצוות
        self._batch_items = []
        self._batch_index = 0

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(50, self._show_initial_message)

    def _show_initial_message(self):
        self.insert_bot_message(
            "👋 היי! אני הסוכן החכם שלך לשופינג 🛒. בכל עת ניתן לשלוח את הודעה ואני אמצא את הסל הזול ביותר, או לשאול אותי כל שאלה בנוגע לקניות, מחירים וסניפים ואוכל 😊")
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()))

    def insert_user_message(self, text: str):
        self._insert_message(text, sender="user")
        conversation_state["history"].append({"role": "user", "content": text})

    def insert_bot_message(self, text: str, return_label: bool = False):
        label = self._insert_message(text, sender="bot", return_label=return_label)
        conversation_state["history"].append({"role": "assistant", "content": text})
        return label

    def _insert_message(self, text: str, sender: str, return_label: bool = False):
        # יצירת בועה (QFrame) – Expanding לרוחב, Preferred לגובה
        bubble = QFrame()
        bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(0, 0, 0, 0)
        bubble_layout.setSpacing(0)

        # אפקט צל קל לבועה
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(6)
        shadow.setOffset(0, 0)
        shadow.setColor(QColor(0, 0, 0, 40))
        bubble.setGraphicsEffect(shadow)

        # ------ כאן יוצרים QLabel ריק, בלי להעביר טקסט לבנאי ------
        label = QLabel()

        # 1. מגדירים פורמט RichText כדי שיוכל לפרש HTML
        label.setTextFormat(Qt.RichText)
        # 2. מאפשרים אינטראקציה של TextBrowser (לינקים ניתנים ללחיצה)
        label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        # 3. מאפשרים פתיחת לינקים חיצוניים בדפדפן
        label.setOpenExternalLinks(True)

        # 4. מוסיפים עטיפה (word wrap) וגופן
        label.setWordWrap(True)
        label.setFont(QFont("Segoe UI", 10))

        # רוחב מקסימלי 60% מרוחב ה־ScrollArea
        max_w = int(self.scroll_area.width() * 0.6)
        label.setMaximumWidth(max_w)

        # 5. עכשיו, אחרי ההגדרות, מכניסים את הטקסט (יכול לכלול תגית <a href=...>)
        label.setText(text)
        # ------ סוף החלק שבו QLabel מתאפיין כ־HTML-Link מנגנן ------

        # המשך העיצוב והיישור בהתאם למי שולח…
        if sender == "user":
            bubble.setStyleSheet("""
                QFrame {
                    background-color: #DCF8C6;
                    border-top-left-radius: 15px;
                    border-top-right-radius: 15px;
                    border-bottom-left-radius: 15px;
                    border-bottom-right-radius: 3px;
                }
                QLabel {
                    padding: 6px;
                    color: #000000;
                }
            """)
            bubble_layout.addWidget(label, alignment=Qt.AlignRight)

            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 10, 0)
            container_layout.addStretch(1)
            container_layout.addWidget(bubble)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, container)
        else:
            bubble.setStyleSheet("""
                QFrame {
                    background-color: #FFFFFF;
                    border-top-left-radius: 15px;
                    border-top-right-radius: 15px;
                    border-bottom-left-radius: 3px;
                    border-bottom-right-radius: 15px;
                }
                QLabel {
                    padding: 6px;
                    color: #000000;
                }
            """)
            bubble_layout.addWidget(label, alignment=Qt.AlignLeft)

            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(10, 0, 0, 0)
            container_layout.addWidget(bubble)
            container_layout.addStretch(1)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, container)

        # גלילה אוטומטית לתחתית אחרי הוספת כל הודעה
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()))

        if return_label:
            return label

    def _process_next_item(self):
        # ברגע שהגענו לסוף – נעבור לסיום החישוב
        if self._batch_index >= len(self._batch_items):
            # מאפסים כדי למנוע ריצות נוספות
            self._batch_items = []
            print("DEBUG: all items done, calling _finish_cart()")
            self._finish_cart()
            return

        it = self._batch_items[self._batch_index]
        try:
            opts = find_product_match(it)
        except Exception as e:
            self.insert_bot_message(f"⚠️ שגיאה בחיפוש מוצר '{it}': {e}")
            opts = []

        if not opts:
            self.insert_bot_message(f"⚠️ לא נמצאו התאמות עבור '{it}'.")
            self._batch_index += 1
            QTimer.singleShot(50, self._process_next_item)
            return

        if len(opts) == 1:
            conversation_state["cart_items"].append(opts[0])
            self._batch_index += 1
            QTimer.singleShot(50, self._process_next_item)
            return

        conversation_state["pending_choices"][it] = opts
        conversation_state["pending_order"].append(it)
        conversation_state["stage"] = "wait_for_selection"

        # בונים את טקסט הבחירה כבר בפורמט HTML עם <br> במקום \n
        html_options = "0. לא לבחור כלום מהרשימה" + "".join(f"<br>{i + 1}. {o}" for i, o in enumerate(opts))
        html_message = f"בחר התאמה ל'{it}':<br>{html_options}"
        self.insert_bot_message(html_message)

    def _finish_cart(self):
        # מסכמים לכל רשת: מוצרים + מחיר יחידני + סה"כ
        totals = calculate_totals(conversation_state["cart_items"])
        self.insert_bot_message("🛒 סיכום עגלה לפי רשת:")

        for chain, total in totals.items():
            # בונים טקסט עם כל מוצר והמחיר שלו ברשת
            text_block = f"{chain}:<br>"
            # רק מוצרים שהמחיר שלהם באמת נמצא
            for prod in conversation_state["cart_items"]:
                pr = df[(df[product_col] == prod) & (df['סופרמרקט'] == chain)][price_col]
                if pr.empty:
                    continue
                price_text = f"₪{pr.iloc[0]:.2f}"
                text_block += f"&nbsp;&nbsp;• {prod}: {price_text}<br>"
            text_block += f"&nbsp;&nbsp;<b>סה\"כ: ₪{total:.2f}</b>"

            # שולחים כ-HTML (כל שורה עם <br>)
            self.insert_bot_message(text_block)

        # הרשת הזולה ביותר
        cheapest, price = min(totals.items(), key=lambda x: x[1])
        self.insert_bot_message(f"✅ הסל הזול ביותר: {cheapest} — ₪{price:.2f}")

        # מציג סניף קרוב וקישור ניווט
        branch_display, maps_url = get_closest_branch(cheapest, conversation_state["location"])
        if branch_display:
            self.insert_bot_message(f"📍 סניף קרוב מומלץ של {cheapest}: {branch_display}")
            if maps_url:
                html_link = f'🚗 <a href="{maps_url}">לחצו כאן כדי לפתוח ניווט ב-Google Maps</a>'
                self.insert_bot_message(html_link)
        else:
            self.insert_bot_message("📍 לא הצלחתי לאתר סניף קרוב של הרשת שביקשת. אנא בדוק את הכתובת ונסה שוב.")

        # מעבר למצב צ'אט רגיל וניקוי העגלה
        conversation_state["stage"] = "chat"
        conversation_state["pending_choices"].clear()
        conversation_state["pending_order"].clear()
        conversation_state["pending_index"] = 0
        conversation_state["cart_items"].clear()
        conversation_state["location"] = ""
        logger.debug("_finish_cart: reset to chat stage")

    def on_send(self):
        ui = self.input_entry.text().strip()
        logger.debug("on_send() called • ui=%r • stage=%r", ui, conversation_state["stage"])
        if not ui:
            logger.debug("on_send: empty ui, returning")
            return

        # 1) הצגת הודעת המשתמש
        self.insert_user_message(ui)
        self.input_entry.clear()
        # 1b) שמירת הודעת המשתמש בהיסטוריה
        conversation_state["history"].append({"role": "user", "content": ui})

        # 2) שליפת intent ו־items מ־GPT
        cls = classify_user_input(ui)
        intent = cls.get("intent", "general")
        items = cls.get("items", [])
        # ─── אם JSON קבע add_items אבל לא הביא פריטים, נסה fuzzy על כל טוקן ───
        if intent == "add_items" and not items:
            for tok in ui.split():
                matches = find_product_match(tok)
                if matches:
                    items.append(matches[0])
        # ────────────────────────────────────────────────────────────────────

        # 3) אם המשתמש ביקש להוסיף מוצרים
        if intent == "add_items" and items:
            new_items = [p for p in items if p not in conversation_state["cart_items"]]
            conversation_state["cart_items"].extend(new_items)
            self.insert_bot_message(f"🛒 הוספתי לעגלה: {', '.join(new_items)}")
            self.insert_bot_message("📌 רוצה להוסיף עוד מוצרים? (כן/לא)")
            conversation_state["stage"] = "awaiting_more_items"
            return


        # 5) אחרת – נמשיך לפי השלב הנוכחי ב־conversation_state
        ui_low = ui.lower()
        st = conversation_state["stage"]
        # ─── שלב 0: אם אנחנו בשלב awaiting_more_items, נטפל בתשובה (כן/לא) ───
        if st == "awaiting_more_items":
            if ui_low in ("כן", "כן,"):
                # חוזרים לשלב איסוף חופשי (כדי להוסיף מוצרים)
                conversation_state["stage"] = "chat"
                self.insert_bot_message("🙂 ספר לי איזה מוצרים נוספים תרצה להוסיף.")
            elif ui_low in ("לא", "לא תודה", "לא, תודה"):
                # עוברים לשלב כתובת
                conversation_state["stage"] = "ask_location"
                self.insert_bot_message("📍 מצוין! אנא הזן את הכתובת שלך בפורמט הבא    (רחוב מספר, עיר):")
            else:
                # כל תשובה אחרת – מבקשים רק 'כן' או 'לא'
                self.insert_bot_message("⚠️ אנא השב רק 'כן' או 'לא'.")
            return
        # ────────────────────────────────────────────────────────────────────

        # ——————————————————————————————
        #   מצב ראשוני (chat)
        if st == "chat":
            if any(trigger in ui_low for trigger in TRIGGERS):
                # – זו הענף של טריגרים, אפשר להשאיר אותו כמו שהוא –
                self.insert_bot_message("מעולה, בוא נתחיל לאסוף את הפריטים שלך 🛒")
                QTimer.singleShot(500, lambda: (
                    conversation_state.update({"stage": "ask_location"}),
                    self.insert_bot_message("📍 אנא הזן את הכתובת שלך (רחוב מספר, עיר):")))
            else:
                # 1) תוספת הבועה "חושב..."
                thinking_label = self.insert_bot_message("🤖 רגע... חושב...", return_label=True)

                # 2) (אופציונלי) אם הו־“thinking” נכנס לטעות להיסטוריה — להסיר אותו
                if conversation_state["history"] \
                        and conversation_state["history"][-1]["role"] == "assistant" \
                        and conversation_state["history"][-1]["content"] == "🤖 רגע... חושב...":
                    conversation_state["history"].pop()

                # 3) לאפשר ל־Qt לצייר את הבועה
                QApplication.processEvents()

                # 4) קריאה סינכרונית ל־API
                response = ask_openai(ui)

                # 5) עדכון הבועה עם הטקסט שקיבלנו
                thinking_label.setText(response)

            return

        # ——————————————————————————————
        #   אחרי "כן/לא" אם הוספנו מוצרים
        if st == "awaiting_more_items":
            if ui_low in ("כן", "כן בבקשה"):
                conversation_state["stage"] = "collect_items"
                self.insert_bot_message("🙂 ספר לי איזה מוצרים נוספים תרצה להוסיף.")
            elif ui_low in ("לא", "לא תודה", "לא, תודה"):
                conversation_state["stage"] = "ask_location"
                self.insert_bot_message("📍 מצוין. אנא הזן את הכתובת שלך (רחוב מספר, עיר):")
            else:
                self.insert_bot_message("אנא השב רק 'כן' או 'לא'.")
            return

        # ——————————————————————————————
        #   שלב כתובת
        if st == "ask_location":
            if "," not in ui:
                self.insert_bot_message("⚠️ נא הזן כתובת מלאה (רחוב, מספר, עיר). נסה שנית: הזנת בפורמט הלא נכון")
                return

            conversation_state["location"] = ui
            conversation_state["stage"] = "process_free_cart"
            self.insert_bot_message("👍 תודה! מעבד כעת את הסל שלך…")

            # מגדירים פעם אחת את רשימת המוצרים לעיבוד
            self._batch_items = conversation_state["cart_items"].copy()
            self._batch_index = 0

            # קוראים לעיבוד
            QTimer.singleShot(50, self._process_next_item)
            return


        if st == "ask_cart":
            # קלט מפורש: מאפסים קודם את העגלה וה־pending
            conversation_state["cart_items"].clear()
            conversation_state["pending_choices"].clear()
            conversation_state["pending_order"].clear()
            conversation_state["pending_index"] = 0

            # בונים את ה־batch מהקלט
            items = [i.strip() for i in ui.replace('-', ',').split(',') if i.strip()]
            self._batch_items = items
            self._batch_index = 0

            QTimer.singleShot(50, self._process_next_item)
            return

        if st == "wait_for_selection":
            try:
                idx = int(ui)
            except ValueError:
                self.insert_bot_message("⚠️ אנא בחר מספר תקני מתוך הרשימה.")
                return

            # רק בודקים פה, לא מתעסקים עדיין ב־pending_order
            key = conversation_state["pending_order"][0]
            opts = conversation_state["pending_choices"][key]

            # טווח חוקי? (כולל 0)
            if idx < 0 or idx > len(opts):
                self.insert_bot_message("⚠️ אנא בחר מספר תקני מתוך הרשימה.")
                return

            # עכשיו בטוח שאפשר להסיר את ה־pending
            conversation_state["pending_order"].pop(0)
            conversation_state["pending_choices"].pop(key)

            # אם בחר 0 — לא מוסיפים כלום
            if idx != 0:
                sel = opts[idx - 1]
                conversation_state["cart_items"].append(sel)
                self.insert_bot_message(f"🛒 הוספתי לעגלה: {sel}")

            # האם נשאר עוד מפתח?
            if conversation_state["pending_order"]:
                next_key = conversation_state["pending_order"][0]
                next_opts = conversation_state["pending_choices"][next_key]
                html = "<br>".join(f"{i + 1}. {o}" for i, o in enumerate(next_opts))
                self.insert_bot_message(
                    f"לאיזה מוצר התכוונת ב'{next_key}'?<br>0. אף אחד<br>{html}"
                )
                return

            # אם אין יותר בחירות ממתינות, ממשיכים לשלב הבא
            self._batch_index += 1
            QTimer.singleShot(50, self._process_next_item)
            return



if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ChatWindow()
    window.show()
    sys.exit(app.exec())
