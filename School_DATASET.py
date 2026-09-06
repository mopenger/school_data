import re
import requests
import asyncio
import random
import time
import pandas as pd
import aiosqlite
from bs4 import BeautifulSoup
from ddgs import DDGS

from langchain_gigachat import GigaChat
from langchain_core.messages import HumanMessage

# ================= CONFIG =================
INPUT_FILE = "выгрузка.xlsx"
OUTPUT_FILE = "output.xlsx"
DB_PATH = "cache.db"

# ================= LLM =================
llm = GigaChat(
    credentials="=====================ТОКЕН=====================", #=====================ТОКЕН==========================================ТОКЕН=====================
    verify_ssl_certs=False,
    temperature=0.2,
    max_tokens=120,
    model="GigaChat"
)

# ================= CACHE =================
async def init_db():
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    """)
    await db.commit()
    return db

async def get_cache(db, key):
    async with db.execute("SELECT value FROM cache WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
        return row[0] if row else None

async def set_cache(db, key, value):
    await db.execute(
        "INSERT OR REPLACE INTO cache VALUES (?, ?)",
        (key, value)
    )
    await db.commit()

# ================= SEARCH =================
def search_links(query):
    for attempt in range(3):
        try:
            links = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=7):
                    url = r.get("href")
                    if url:
                        links.append(url)
            return links
        except Exception as e:
            print(f"❌ DDGS error (attempt {attempt+1}): {e}")
            time.sleep(2)
    return []

# ================= FETCH =================
def fetch_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)

        if r.status_code != 200:
            return ""

        soup = BeautifulSoup(r.text, "lxml")

        for t in soup(["script", "style"]):
            t.decompose()

        return soup.get_text(" ", strip=True)[:3000]
    except Exception:
        return ""

# ================= FILTER =================
def is_relevant(text):
    t = text.lower()
    return any(k in t for k in ["школ", "учащ", "контингент", "обуча"])

# ================= PARSE =================
def extract_regex(text):
    patterns = [
        r'(\d{2,4})\s*(учащ|ученик|обуча)',
        r'численность.{0,20}(\d{2,4})',
        r'контингент.{0,20}(\d{2,4})'
    ]

    for p in patterns:
        m = re.findall(p, text.lower())
        for g in m:
            # g может быть кортежем или строкой, берем первое числовое значение
            val_str = g[0] if isinstance(g, tuple) else g
            try:
                val = int(val_str)
                if 50 <= val <= 10000:
                    return val
            except ValueError:
                continue
    return None

# ================= LLM =================
def ask_llm(context, school, city):
    prompt = f"""
Найди численность учащихся школы "{school}" в городе "{city}".

- ответ только число
- если нет данных — 0

Контекст:
{context}
"""
    try:
        res = llm.invoke([HumanMessage(content=prompt)])
        nums = re.findall(r"\d+", res.content)
        return int(nums[0]) if nums else None
    except Exception:
        return None

# ================= CORE =================
async def process_row(row, db):
    school = str(row["school"])
    city = str(row["city"])
    key = f"{school}_{city}"

    # 🔥 1. Проверка кеша
    cached = await get_cache(db, key)
    if cached is not None:
        print(f"⚡ cache hit: {school}")
        return cached

    print(f"🌐 processing: {school}")

    # 🔥 2. Задержка (анти-бан)
    await asyncio.sleep(random.uniform(1, 2))

    query = f"{school} {city} численность учащихся"
    links = search_links(query)

    texts = []
    for url in links:
        text = fetch_text(url)
        if not text:
            continue
        if not is_relevant(text):
            continue
        texts.append(text)

    if not texts:
        await set_cache(db, key, 0)
        return 0

    texts.sort(key=lambda x: x.count("учащ"), reverse=True)
    context = "\n\n".join(texts[:3])[:4000]

    val_r = extract_regex(context)
    val_l = ask_llm(context, school, city)

    if val_r and val_l:
        result = val_r if abs(val_r - val_l) < 100 else val_l
    else:
        result = val_l or val_r or 0

    # 🔥 3. Сохраняем сразу
    await set_cache(db, key, result)
    return result

# ================= MAIN =================
async def main():
    df = pd.read_excel(INPUT_FILE)

    # 🔥 не трогаем уже заполненные строки
    if "students_count" not in df.columns:
        df["students_count"] = None

    db = await init_db()
    results = []

    for i, row in df.iterrows():
        # 🔥 пропуск если уже есть значение
        if pd.notna(row["students_count"]) and row["students_count"] != 0:
            results.append(row["students_count"])
            continue

        try:
            val = await process_row(row, db)
        except Exception as e:
            print(f"❌ error row {i}: {e}")
            val = 0

        results.append(val)

    df["students_count"] = results
    df.to_excel(OUTPUT_FILE, index=False)

    await db.close()
    print("✅ DONE")

# ================= RUN =================
if __name__ == "__main__":
    asyncio.run(main())
