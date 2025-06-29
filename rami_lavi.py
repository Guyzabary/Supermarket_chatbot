import pandas as pd
import re
from collections import Counter

# ========== שלב 1: נורמליזציה של שמות המוצרים ==========

irrelevant_words = {"בטעם", "עם", "מארז", "יחידות", "יח", "חדש", "ללא", "extra"}


def normalize_name(name):
    if pd.isna(name): pipreqs . --force

        return ""

    name = str(name).lower()

    # הסרת כמויות ויחידות
    name = re.sub(r"\b\d+\.?\d*\s?(מ?ל|ליטר|ק?ג|גרם|שקית|יחידות?|יח'?)\b", "", name)

    # הסרת מילים קצרות מאוד / בודדות
    name = re.sub(r"\b[0-9א-ת]{1,2}\b", "", name)

    # הסרת תווים לא רלוונטיים
    name = re.sub(r"[^\w\s]", "", name)

    # הסרת מילים לא תורמות
    tokens = [word for word in name.split() if word not in irrelevant_words]

    return " ".join(tokens).strip()


# ========== שלב 2: טעינת הקובץ וסיווג לפי מילון (ריק בשלב זה) ==========

# טען את הקובץ
df = pd.read_csv("רמי לוי.csv", encoding="utf-8-sig")

# צור עמודת שם מנורמל
df["NormalizedName"] = df["ItemName"].apply(normalize_name)

# מילון ראשוני ריק (נעדכן אותו מאקסל מאוחר יותר)
category_keywords = {}


# פונקציית סיווג
def classify_product(name):
    for category, keywords in category_keywords.items():
        for keyword in keywords:
            if keyword in name:
                return category
    return "לבדיקה ידנית"


# סיווג ראשוני
df["מחלקה"] = df["NormalizedName"].apply(classify_product)

# שמור את הקובץ המסווג
df.to_csv("רמי לוי - סיווג ראשוני.csv", index=False, encoding="utf-8-sig")
print("✔ נשמר: רמי לוי - סיווג ראשוני.csv")

# ========== שלב 3: הפקת מילים נפוצות מתוך 'לבדיקה ידנית' לצורך הרחבת המילון ==========

unclassified = df[df["מחלקה"] == "לבדיקה ידנית"]

words = []
for name in unclassified["NormalizedName"].dropna():
    tokens = re.findall(r'\b\w+\b', name)
    words.extend(tokens)

common_words = Counter(words).most_common(300)
common_df = pd.DataFrame(common_words, columns=["מילה", "כמות"])

# שמור לקובץ אקסל שתמלא בו מחלקות
common_df.to_excel("מילים לסיווג חדש.xlsx", index=False)
print("✔ נשמר: מילים לסיווג חדש.xlsx (נא מלא עמודת 'מחלקה')")

# ========== שלב 4: לאחר שמילאת באקסל את המחלקות – נבנה את המילון מחדש מהקובץ ==========

# קרא את המילון המעודכן
word_map_df = pd.read_excel("מילים לסיווג חדש.xlsx")

# בניית מילון דינמי
category_keywords = {}
for _, row in word_map_df.iterrows():
    word = str(row["מילה"]).strip()
    category = str(row["מחלקה"]).strip()
    if category and category != "nan":
        category_keywords.setdefault(category, []).append(word)

# סיווג מחדש לפי המילון החדש
df["מחלקה"] = df["NormalizedName"].apply(classify_product)

# שמירה לקובץ סופי
df.to_csv("רמי לוי - מסווג סופי.csv", index=False, encoding="utf-8-sig")
print("✔ נשמר: רמי לוי - מסווג סופי.csv עם המילון המורחב שלך")
