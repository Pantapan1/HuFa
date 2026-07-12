import telebot
import sqlite3
import os
import re
import json
import time
import random
import logging
import html as html_module
import requests
from telebot import types
from groq import Groq
from collections import Counter, deque

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, 'hupha_bot.db')

# ============================================================
# ЗАГРУЗКА СЕКРЕТОВ ИЗ .env (токены больше не хранятся в коде)
# ============================================================
def _load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_env = _load_env(os.path.join(BASE_DIR, '.env'))
TOKEN = os.environ.get('BOT_TOKEN') or _env.get('BOT_TOKEN')
GROQ_API_KEY = os.environ.get('GROQ_KEY') or _env.get('GROQ_KEY')
ADMIN_ID = int(os.environ.get('ADMIN_ID') or _env.get('ADMIN_ID', 0))
SERPAPI_KEY = os.environ.get('SERPAPI_KEY') or _env.get('SERPAPI_KEY', '')

if not TOKEN or not GROQ_API_KEY or not ADMIN_ID:
    raise RuntimeError(
        "❌ Не найдены BOT_TOKEN / GROQ_KEY / ADMIN_ID.\n"
        "Заполни файл .env рядом с main.py, например:\n"
        "BOT_TOKEN=...\nGROQ_KEY=...\nADMIN_ID=123456789"
    )

# ============================================================
# ЛОГИРОВАНИЕ (вместо print — пишем в файл и в консоль)
# ============================================================
LOG_FILE = os.path.join(BASE_DIR, 'bot.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('hufa_bot')

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# Хранилища состояний и данных
user_states = {}
temp_data = {}
temp_learning = {}
message_context = {}
story_parts = {}
story_tellers = {}
broadcast_data = {}
quiz_data = {}
dialogue_learning = {}
rp_sessions = {}
rp_pending = {}
session_contexts = {}

# БУФЕР ИСТОРИИ ДИАЛОГОВ ДЛЯ КОНТЕКСТНЫХ ОТВЕТОВ
chat_memory = {}

# Глобальные блокировки для потокобезопасности
import threading
db_lock = threading.Lock()
memory_lock = threading.Lock()

CATEGORY_EMOJI = {
    'персонаж': '👤', 'локация': '🏛️', 'предмет': '💎',
    'фракция': '🎭', 'событие': '📜', 'организация': '🏢',
    'существо': '🐉', 'магия': '🔮', 'общее': '📚'
}

ALLOWED_COMMANDS = ['/getid', '/roll', '/create', '/start', '/gm_suggest', '/npc', '/location',
                     '/encounter', '/oracle', '/puzzle', '/prophecy', '/dialogue', '/quest',
                     '/rp_start', '/rp_narrate', '/rp_stop', '/rp_mode', '/users',
                     '/search', '/bookmark', '/recap', '/give', '/additem', '/ban', '/unban']

def clean_text(text, is_key=False):
    if not text: return ""
    text = text.strip()
    if is_key: text = re.sub(r'[\[\]\(\)\{\}\.\,\!\?\:\;\-\"\']', '', text)
    return text

def execute_db(query, params=(), is_select=False):
    try:
        with db_lock:
            with sqlite3.connect(DB_NAME, timeout=15) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()
                cursor.execute(query, params)
                if is_select: return cursor.fetchall()
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка БД: {e} | Запрос: {query[:200]}")
        return []

def init_db():
    # WAL значительно снижает блокировки при параллельных запросах
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
    except Exception as e:
        logger.warning(f"Не удалось включить WAL: {e}")

    execute_db('''CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, name TEXT, bio TEXT, photo TEXT, хуфа INTEGER DEFAULT 0, рубли INTEGER DEFAULT 100, last_daily TIMESTAMP)''')
    execute_db('''CREATE TABLE IF NOT EXISTS wiki (keyword TEXT PRIMARY KEY, description TEXT, photo_id TEXT, category TEXT DEFAULT 'общее')''')
    execute_db('''CREATE TABLE IF NOT EXISTS wiki_links (id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, target_key TEXT NOT NULL, link_type TEXT NOT NULL, UNIQUE(source_key, target_key, link_type))''')
    execute_db('''CREATE TABLE IF NOT EXISTS stories (id INTEGER PRIMARY KEY AUTOINCREMENT, story_name TEXT NOT NULL, part_number INTEGER NOT NULL, content TEXT NOT NULL, content_type TEXT DEFAULT 'text', file_id TEXT, original_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    execute_db('''CREATE TABLE IF NOT EXISTS broadcast_users (user_id INTEGER, chat_id INTEGER, PRIMARY KEY (user_id, chat_id))''')
    execute_db('''CREATE TABLE IF NOT EXISTS rp_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, gm_id INTEGER NOT NULL, session_name TEXT, context TEXT, status TEXT DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    execute_db('''CREATE TABLE IF NOT EXISTS rp_channels (chat_id INTEGER NOT NULL, mode TEXT DEFAULT 'silent', PRIMARY KEY (chat_id))''')
    execute_db('''CREATE TABLE IF NOT EXISTS chat_contexts (chat_id INTEGER PRIMARY KEY, context_summary TEXT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # --- Новые таблицы: экономика, модерация, закладки ---
    execute_db('''CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT, price INTEGER DEFAULT 0, currency TEXT DEFAULT 'рубли', emoji TEXT DEFAULT '🎁')''')
    execute_db('''CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER NOT NULL, item_id INTEGER NOT NULL, qty INTEGER DEFAULT 1, PRIMARY KEY (user_id, item_id))''')
    execute_db('''CREATE TABLE IF NOT EXISTS blocked_users (user_id INTEGER PRIMARY KEY, reason TEXT, blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    execute_db('''CREATE TABLE IF NOT EXISTS bookmarks (user_id INTEGER NOT NULL, keyword TEXT NOT NULL, PRIMARY KEY (user_id, keyword))''')
    # --- Индексы для ускорения частых запросов ---
    execute_db('''CREATE INDEX IF NOT EXISTS idx_wiki_category ON wiki(category)''')
    execute_db('''CREATE INDEX IF NOT EXISTS idx_stories_name ON stories(story_name)''')
    execute_db('''CREATE INDEX IF NOT EXISTS idx_players_хуфа ON players(хуфа)''')
    execute_db('''CREATE INDEX IF NOT EXISTS idx_players_рубли ON players(рубли)''')

def safe_html(text):
    safe = html_module.escape(text)
    for tag in ['b', 'i', 'u', 'code']:
        safe = safe.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return safe

def safe_send(chat_id, text, photo_id=None, keyword=None):
    if not text: return
    try:
        safe_text = safe_html(text)
        if photo_id:
            if len(safe_text) <= 1000:
                msg = bot.send_photo(chat_id, photo_id, caption=safe_text, parse_mode="HTML")
            else:
                bot.send_photo(chat_id, photo_id)
                msg = bot.send_message(chat_id, safe_text, parse_mode="HTML")
        else:
            msg = bot.send_message(chat_id, safe_text, parse_mode="HTML")
        if msg and keyword:
            if chat_id not in message_context: message_context[chat_id] = {}
            message_context[chat_id][msg.message_id] = keyword
        return msg
    except Exception as e:
        print(f"❌ Ошибка safe_send: {e}")
        try: bot.send_message(chat_id, text)
        except: pass

def save_to_memory(chat_id, user_message, bot_answer):
    with memory_lock:
        if chat_id not in chat_memory:
            chat_memory[chat_id] = deque(maxlen=10)
        chat_memory[chat_id].append({
            'user': user_message,
            'bot_answer': bot_answer,
            'timestamp': time.time()
        })

def get_memory_context(chat_id):
    with memory_lock:
        if chat_id not in chat_memory or not chat_memory[chat_id]:
            return ""
        recent = list(chat_memory[chat_id])[-5:]
        context_parts = []
        for entry in recent:
            context_parts.append(f"Игрок спросил: {entry['user'][:200]}\nБот ответил: {entry['bot_answer'][:200]}")
        return "\n".join(context_parts)

def search_wiki_with_context(message):
    query = message.text
    chat_id = message.chat.id
    
    memory_context = get_memory_context(chat_id)
    
    all_wiki = execute_db("SELECT keyword, description, photo_id FROM wiki", (), True)
    if not all_wiki: return None, None, None
    
    found_photo = target_data = current_key = None
    clean_query = clean_text(query, is_key=True).lower()
    
    clarifying_words = {"а", "но", "и", "или", "ещё", "тоже", "также", "тогда", "потом", "затем", "после"}
    query_words = set(clean_query.split())
    is_clarifying = bool(query_words & clarifying_words) or len(query_words) <= 2
    
    candidates = []
    for kw, desc, photo in all_wiki:
        weight = 0
        if kw.lower() in clean_query: weight = 100 + len(kw)
        else:
            kw_words = set(kw.lower().split())
            if kw_words & query_words: weight += len(kw_words & query_words) * 5
        if weight > 0: candidates.append((weight, kw, desc, photo))
    
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, current_key, target_data, found_photo = candidates[0]
    elif is_clarifying and memory_context:
        last_topic = None
        with memory_lock:
            if chat_id in chat_memory and chat_memory[chat_id]:
                last_bot_answer = chat_memory[chat_id][-1]['bot_answer']
                for kw, desc, photo in all_wiki:
                    if kw.lower() in last_bot_answer.lower():
                        current_key = kw
                        target_data = desc
                        found_photo = photo
                        break
    
    if not target_data: return None, None, None
    
    if len(target_data) > 30000: target_data = target_data[:30000] + "..."
    
    try:
        system_prompt = f"Ты — мудрый Хранитель знаний. Тема: {current_key}."
        if memory_context:
            system_prompt += f"\n\nПредыдущий разговор:\n{memory_context}"
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"ДОСЬЕ:\n{target_data}\n\nВОПРОС ИГРОКА: {query}\n\nОтветь на вопрос, учитывая контекст предыдущего разговора если это уточнение."}
            ],
            temperature=0.3,
            max_tokens=500
        )
        answer = completion.choices[0].message.content
        links_text = get_links_text(current_key)
        if links_text: answer += f"\n\n🕸 <b>Связи:</b>\n{links_text}"
        
        save_to_memory(chat_id, query, answer)
        
        return answer, found_photo, current_key
    except Exception as e:
        print(f"❌ Ошибка AI: {e}")
        if target_data:
            answer = f"📚 <b>{current_key.capitalize()}</b>\n\n{target_data[:1000]}{'...' if len(target_data) > 1000 else ''}"
            links_text = get_links_text(current_key)
            if links_text: answer += f"\n\n🕸 <b>Связи:</b>\n{links_text}"
            save_to_memory(chat_id, query, answer)
            return answer, found_photo, current_key
        return "🔮 Библиотека временно недоступна...", None, None

print(">>> Модуль 1 загружен (инициализация)")
def web_search(query, num_results=3):
    if not SERPAPI_KEY:
        try:
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "system", "content": "Ты — поисковый помощник. Найди информацию по запросу и ответь кратко на русском."}, {"role": "user", "content": f"Найди информацию: {query}"}],
                temperature=0.3, max_tokens=500
            )
            return completion.choices[0].message.content.strip()
        except: return None
    try:
        url = "https://serpapi.com/search"
        params = {"q": query, "hl": "ru", "gl": "ru", "num": num_results, "api_key": SERPAPI_KEY}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        results = []
        for r in data.get("organic_results", [])[:num_results]:
            results.append(f"• {r.get('title', '')}: {r.get('snippet', '')[:200]}")
        return "\n".join(results) if results else None
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return None

def get_chat_context(chat_id):
    context = execute_db("SELECT context_summary FROM chat_contexts WHERE chat_id = ?", (chat_id,), True)
    if context: return context[0][0]
    return None

def set_chat_context(chat_id, context_text):
    execute_db("INSERT OR REPLACE INTO chat_contexts (chat_id, context_summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)", (chat_id, context_text))

def analyze_chat_history_for_context(chat_id, admin_text=None):
    messages_for_analysis = []
    if admin_text: messages_for_analysis.append(admin_text)
    wiki_entries = execute_db("SELECT keyword, description FROM wiki", (), True)
    if wiki_entries:
        wiki_text = "\n".join([f"• {kw}: {desc[:200]}" for kw, desc in wiki_entries[:20]])
        messages_for_analysis.append(f"База знаний бота:\n{wiki_text}")
    chat_context = execute_db("SELECT context_summary FROM chat_contexts WHERE chat_id = ?", (chat_id,), True)
    if chat_context: messages_for_analysis.append(f"Предыдущий контекст: {chat_context[0][0]}")
    if not messages_for_analysis and not admin_text: return None
    combined_text = "\n\n".join(messages_for_analysis)
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — аналитик ролевых игр. Проанализируй информацию и определи тему, сеттинг, ключевых персонажей и возможный сюжет для РП-сессии. Ответь кратко на русском языке, 3-5 предложений."}, {"role": "user", "content": f"Проанализируй для создания РП-сессии:\n{combined_text[:4000]}"}], temperature=0.4, max_tokens=300)
        context_summary = completion.choices[0].message.content.strip()
        if admin_text:
            search_results = web_search(admin_text)
            if search_results: context_summary += f"\n\n🌐 Из интернета:\n{search_results[:500]}"
        execute_db("INSERT OR REPLACE INTO chat_contexts (chat_id, context_summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)", (chat_id, context_summary))
        return context_summary
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        if admin_text:
            execute_db("INSERT OR REPLACE INTO chat_contexts (chat_id, context_summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)", (chat_id, admin_text))
            return admin_text
        return None

def start_rp_session(chat_id, gm_id, session_name="РП-сессия", context=None):
    if not context: context = get_chat_context(chat_id)
    if not context: return None, "no_context"
    execute_db("INSERT INTO rp_sessions (chat_id, gm_id, session_name, context) VALUES (?, ?, ?, ?)", (chat_id, gm_id, session_name, context))
    session_db_id = execute_db("SELECT last_insert_rowid()", (), True)[0][0]
    rp_sessions[chat_id] = {'gm_id': gm_id, 'name': session_name, 'context': [], 'active': True, 'thread_id': None, 'db_id': session_db_id, 'session_context': context}
    session_contexts[chat_id] = context
    execute_db("INSERT OR REPLACE INTO rp_channels (chat_id, mode) VALUES (?, 'active')", (chat_id,))
    return context, "ok"

def process_rp_message(chat_id, user_id, user_text, user_name):
    if chat_id not in rp_sessions or not rp_sessions[chat_id]['active']: return False
    gm_id = rp_sessions[chat_id]['gm_id']
    thread_info = f"\n📌 Тема: {rp_sessions[chat_id].get('thread_id')}" if rp_sessions[chat_id].get('thread_id') else ""
    context_info = f"\n📖 Контекст: {rp_sessions[chat_id].get('session_context', 'Не задан')[:200]}"
    gm_msg = f"🎭 <b>РП · {rp_sessions[chat_id]['name']}</b>{thread_info}{context_info}\n👤 <b>{user_name}</b> [ID: <code>{user_id}</code>]:\n{user_text}\n\n<i>Ответь на это сообщение, чтобы ответить игроку</i>"
    rp_sessions[chat_id]['context'].append({'user_id': user_id, 'user_name': user_name, 'text': user_text, 'timestamp': time.time()})
    sent_msg = bot.send_message(gm_id, gm_msg, parse_mode="HTML")
    rp_pending[chat_id] = rp_pending.get(chat_id, {})
    rp_pending[chat_id][sent_msg.message_id] = user_id
    return True

def gm_reply_to_player(gm_id, reply_text, original_msg_id):
    for chat_id, pending in rp_pending.items():
        if original_msg_id in pending:
            polished = polish_rp_text(reply_text)
            thread_id = rp_sessions[chat_id].get('thread_id')
            bot.send_message(chat_id, polished, parse_mode="HTML", message_thread_id=thread_id)
            return True
    return False

def gm_narrate(chat_id, gm_text):
    if chat_id not in rp_sessions: return False
    polished = polish_rp_text(gm_text)
    thread_id = rp_sessions[chat_id].get('thread_id')
    bot.send_message(chat_id, f"📖 {polished}", parse_mode="HTML", message_thread_id=thread_id)
    rp_sessions[chat_id]['context'].append({'user_id': 'gm', 'user_name': 'Мастер', 'text': gm_text, 'timestamp': time.time()})
    return True

def polish_rp_text(text):
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — литературный редактор. Исправь грамматику, сделай текст атмосферным. НЕ добавляй действий за игроков. Ответь ТОЛЬКО текстом без HTML."}, {"role": "user", "content": f"ТЕКСТ:\n{text}\n\nОТРЕДАКТИРУЙ:"}], temperature=0.5, max_tokens=1000)
        return completion.choices[0].message.content.strip()
    except: return text

def stop_rp_session(chat_id):
    if chat_id in rp_sessions: rp_sessions[chat_id]['active'] = False
    execute_db("UPDATE rp_sessions SET status = 'finished' WHERE chat_id = ? AND status = 'active'", (chat_id,))
    execute_db("INSERT OR REPLACE INTO rp_channels (chat_id, mode) VALUES (?, 'silent')", (chat_id,))
    if chat_id in session_contexts: del session_contexts[chat_id]

def migrate_db():
    migrations = [
        ("stories", "original_content", "TEXT"),
        ("wiki", "category", "TEXT DEFAULT 'общее'"),
        ("rp_sessions", "context", "TEXT"),
        ("players", "last_daily", "TIMESTAMP"),
    ]
    for table, column, col_type in migrations:
        try:
            columns = execute_db(f"PRAGMA table_info({table})", (), True)
            if column not in [col[1] for col in columns]: execute_db(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception as e:
            logger.warning(f"Миграция {table}.{column} пропущена: {e}")

def is_blocked(uid):
    return bool(execute_db("SELECT 1 FROM blocked_users WHERE user_id = ?", (uid,), True))

def groq_complete(system_prompt, user_prompt, temperature=0.4, max_tokens=800, model="llama-3.1-8b-instant", retries=2):
    """Единая обёртка для вызовов Groq с ретраями и логированием ошибок."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=temperature,
                max_tokens=max_tokens
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    logger.error(f"Groq недоступен после {retries + 1} попыток: {last_err}")
    return None

def get_all_users():
    users = set()
    for p in execute_db("SELECT user_id FROM players", (), True): users.add(p[0])
    for b in execute_db("SELECT DISTINCT user_id FROM broadcast_users", (), True): users.add(b[0])
    return list(users)

def broadcast_message(admin_id, text, photo_id=None):
    users = get_all_users()
    success = failed = 0
    for user_id in users:
        try:
            if photo_id: bot.send_photo(user_id, photo_id, caption=text, parse_mode="HTML")
            else: bot.send_message(user_id, text, parse_mode="HTML")
            success += 1; time.sleep(0.1)
        except: failed += 1
    return success, failed

def add_wiki_link(source, target, link_type):
    execute_db("INSERT OR IGNORE INTO wiki_links (source_key, target_key, link_type) VALUES (?, ?, ?)", (source.lower(), target.lower(), link_type))

def get_wiki_links(keyword):
    return execute_db("SELECT source_key, target_key, link_type FROM wiki_links WHERE source_key = ? OR target_key = ?", (keyword.lower(), keyword.lower()), True)

def get_links_text(keyword):
    links = get_wiki_links(keyword)
    if not links: return None
    link_types = {'враг': '⚔️ Враг', 'друг': '🤝 Друг', 'союзник': '🛡️ Союзник', 'находится_в': '📍 Находится в', 'владеет': '💎 Владеет', 'часть': '🧩 Часть'}
    lines = []
    for source, target, ltype in links:
        icon = link_types.get(ltype, '🔗')
        if source.lower() == keyword.lower(): lines.append(f"{icon} → {target.capitalize()}")
        else: lines.append(f"{icon} ← {source.capitalize()}")
    return "\n".join(lines)

def get_wiki_by_category(category):
    return execute_db("SELECT keyword, description, photo_id FROM wiki WHERE category = ? ORDER BY keyword", (category,), True)

def get_categories_stats():
    return execute_db("SELECT category, COUNT(*) as cnt FROM wiki GROUP BY category ORDER BY cnt DESC", (), True)

def get_wiki_info(keyword):
    result = execute_db("SELECT keyword, description, photo_id, category FROM wiki WHERE keyword = ?", (keyword.lower(),), True)
    return result[0] if result else None

def get_random_lore():
    all_wiki = execute_db("SELECT keyword, description, photo_id FROM wiki", (), True)
    return random.choice(all_wiki) if all_wiki else None

def generate_quiz():
    all_wiki = execute_db("SELECT keyword, description FROM wiki", (), True)
    if len(all_wiki) < 4: return None
    correct = random.choice(all_wiki)
    wrong = random.sample([k for k, d in all_wiki if k != correct[0]], min(3, len(all_wiki)-1))
    options = wrong + [correct[0]]
    random.shuffle(options)
    return {'question': f"❓ <b>Вопрос:</b> {correct[1][:200]}...\n\n<b>О ком/чём идёт речь?</b>", 'correct': correct[0], 'options': options}

def get_lore_stats():
    total = execute_db("SELECT COUNT(*) FROM wiki", (), True)
    links_count = execute_db("SELECT COUNT(*) FROM wiki_links", (), True)
    stats = f"📊 <b>Статистика:</b>\n📚 Знаний: {total[0][0] if total else 0}\n🔗 Связей: {links_count[0][0] if links_count else 0}\n\n<b>Категории:</b>\n"
    for cat, count in execute_db("SELECT category, COUNT(*) FROM wiki GROUP BY category ORDER BY COUNT(*) DESC", (), True):
        stats += f"{CATEGORY_EMOJI.get(cat, '📚')} {cat}: {count}\n"
    return stats

def check_lore_conflicts():
    all_wiki = execute_db("SELECT keyword, description FROM wiki", (), True)
    conflicts = []
    for kw1, desc1 in all_wiki:
        for kw2, desc2 in all_wiki:
            if kw1 >= kw2: continue
            links = get_wiki_links(kw1)
            if links and any(kw2.lower() in [l[0], l[1]] for l in links) and abs(len(desc1) - len(desc2)) > 1000:
                conflicts.append(f"⚠️ {kw1} и {kw2} связаны, но описания различаются")
    return conflicts[:5] if conflicts else None

def edit_wiki_keyword(old_keyword, new_keyword):
    try:
        if not execute_db("SELECT keyword FROM wiki WHERE keyword = ?", (old_keyword.lower(),), True): return False, f"❌ Ключ «{old_keyword}» не найден!"
        if execute_db("SELECT keyword FROM wiki WHERE keyword = ?", (new_keyword.lower(),), True): return False, f"❌ Ключ «{new_keyword}» уже существует!"
        execute_db("UPDATE wiki SET keyword = ? WHERE keyword = ?", (new_keyword.lower(), old_keyword.lower()))
        execute_db("UPDATE wiki_links SET source_key = ? WHERE source_key = ?", (new_keyword.lower(), old_keyword.lower()))
        execute_db("UPDATE wiki_links SET target_key = ? WHERE target_key = ?", (new_keyword.lower(), old_keyword.lower()))
        return True, f"✅ Ключ изменён: «{old_keyword}» → «{new_keyword}»"
    except: return False, "❌ Ошибка"

def edit_wiki_photo(keyword, new_photo_id=None):
    try:
        if not execute_db("SELECT keyword FROM wiki WHERE keyword = ?", (keyword.lower(),), True): return False, f"❌ Ключ «{keyword}» не найден!"
        if new_photo_id is None: execute_db("UPDATE wiki SET photo_id = NULL WHERE keyword = ?", (keyword.lower(),)); return True, "✅ Фото удалено!"
        execute_db("UPDATE wiki SET photo_id = ? WHERE keyword = ?", (new_photo_id, keyword.lower())); return True, "✅ Фото обновлено!"
    except: return False, "❌ Ошибка"

def find_story_by_name(story_name):
    for s in execute_db("SELECT DISTINCT story_name FROM stories", (), True):
        if s[0].lower() == story_name.lower(): return s[0]
    return None

def get_story_parts(story_name):
    actual = find_story_by_name(story_name)
    return execute_db("SELECT part_number, content, content_type, file_id FROM stories WHERE story_name = ? ORDER BY part_number", (actual,), True) if actual else []

def get_all_stories():
    return execute_db("SELECT DISTINCT story_name, COUNT(*) FROM stories GROUP BY story_name", (), True)

def delete_story(story_name):
    actual = find_story_by_name(story_name)
    if actual: execute_db("DELETE FROM stories WHERE story_name = ?", (actual,))

def get_story_count(story_name):
    actual = find_story_by_name(story_name)
    if not actual: return 0
    res = execute_db("SELECT COUNT(*) FROM stories WHERE story_name = ?", (actual,), True)
    return res[0][0] if res else 0

def save_story_part(story_name, part_number, content, content_type='text', file_id=None, original_content=None):
    try: execute_db("INSERT INTO stories (story_name, part_number, content, content_type, file_id, original_content) VALUES (?, ?, ?, ?, ?, ?)", (story_name, part_number, content, content_type, file_id, original_content))
    except: execute_db("INSERT INTO stories (story_name, part_number, content, content_type, file_id) VALUES (?, ?, ?, ?, ?)", (story_name, part_number, content, content_type, file_id))

def format_profile(user_id):
    res = execute_db('SELECT name, bio, photo, хуфа, рубли FROM players WHERE user_id = ?', (user_id,), True)
    if not res: return None, None
    p = res[0]
    caption = f"<b>Герой</b>\n🆔 ID: <code>{user_id}</code>\n🎭 <b>Имя:</b> {p[0]}\n🧪 <b>Хуфа:</b> {p[3]}\n💰 <b>Рубли:</b> {p[4]}\n\n📖 <b>Био:</b>\n{p[1]}"
    return caption, p[2]

def send_story_part(chat_id, part, part_num, total_parts, story_name):
    content, content_type, file_id = part[1], part[2], part[3]
    safe_content = safe_html(content)
    header = f"📖 <b>{story_name}</b> [{part_num}/{total_parts}]\n\n"
    try:
        if content_type == 'text': bot.send_message(chat_id, header + safe_content, parse_mode="HTML")
        elif content_type == 'photo' and file_id: bot.send_photo(chat_id, file_id, caption=header + safe_content[:1000], parse_mode="HTML")
        elif content_type == 'video' and file_id: bot.send_video(chat_id, file_id, caption=header + safe_content[:1000], parse_mode="HTML")
        else: bot.send_message(chat_id, header + safe_content, parse_mode="HTML")
    except:
        try: bot.send_message(chat_id, f"📖 {story_name} [{part_num}/{total_parts}]\n\n{content[:4000]}")
        except: pass

def split_text_for_ai(text, max_chars=3000):
    if len(text) <= max_chars: return [text]
    parts, current = [], ""
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        if len(current) + len(sentence) < max_chars: current += sentence + " "
        else:
            if current: parts.append(current.strip())
            current = sentence + " "
    if current: parts.append(current.strip())
    return parts

def ai_categorize_keyword(keyword, description=""):
    if not description or len(description.strip()) < 10: description = keyword
    text_parts = split_text_for_ai(description, max_chars=2500)
    all_categories = []
    for part in text_parts:
        try:
            completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": f"Определи категорию:\nНАЗВАНИЕ: {keyword}\nТЕКСТ: {part[:3000]}\n\nКатегории: персонаж, локация, предмет, фракция, событие, организация, существо, магия, общее\n\nВерни ОДНО слово:"}], temperature=0.1, max_tokens=10)
            category = re.sub(r'[^а-яё]', '', completion.choices[0].message.content.strip().lower())
            all_categories.append(category if category in CATEGORY_EMOJI else 'общее')
            if len(text_parts) > 1: time.sleep(0.2)
        except: all_categories.append('общее')
    return Counter(all_categories).most_common(1)[0][0] if all_categories else 'общее'

def auto_categorize_all():
    count = 0
    for keyword, description in execute_db("SELECT keyword, description FROM wiki", (), True):
        category = ai_categorize_keyword(keyword, description)
        if category: execute_db("UPDATE wiki SET category = ? WHERE keyword = ?", (category, keyword.lower())); count += 1
        time.sleep(0.3)
    return count

def generate_wiki_description(keyword, context_hint=""):
    all_wiki = execute_db("SELECT keyword, description FROM wiki", (), True)
    wiki_context = "\n".join([f"• {kw}: {desc[:300]}" for kw, desc in random.sample(all_wiki, min(5, len(all_wiki)))]) if all_wiki else ""
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — Хранитель знаний."}, {"role": "user", "content": f"Создай описание: «{keyword}»\n\n{wiki_context}"}], temperature=0.5, max_tokens=1500)
        return completion.choices[0].message.content.strip()
    except: return None

def analyze_vs_battle(char1, char2):
    all_wiki = execute_db("SELECT keyword, description FROM wiki", (), True)
    char1_data = char2_data = ""
    other_lore = []
    for kw, desc in all_wiki:
        if kw.lower() == char1.lower(): char1_data = f"• {kw}: {desc}"
        elif kw.lower() == char2.lower(): char2_data = f"• {kw}: {desc}"
        elif len(other_lore) < 5: other_lore.append(f"• {kw}: {desc[:200]}")
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — Верховный Арбитр."}, {"role": "user", "content": f"Битва: {char1} vs {char2}\n\n{char1}: {char1_data or 'Нет данных'}\n{char2}: {char2_data or 'Нет данных'}\n\nЛОР:\n{chr(10).join(other_lore) if other_lore else 'Лор пуст.'}"}], temperature=0.6, max_tokens=2000)
        return completion.choices[0].message.content.strip()
    except: return None

def polish_story_text(raw_text, story_name, part_num):
    if len(raw_text) < 50: return raw_text
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — летописец."}, {"role": "user", "content": f"{raw_text}\n\nОТРЕДАКТИРУЙ:"}], temperature=0.4, max_tokens=4000)
        return completion.choices[0].message.content.strip()
    except: return raw_text

def polish_full_story(story_name):
    actual_name = find_story_by_name(story_name)
    if not actual_name: return None
    parts = get_story_parts(actual_name)
    if not parts: return None
    text_parts = [(pn, c) for pn, c, ct, f in parts if ct == 'text' and len(c.strip()) > 10]
    if not text_parts: return None
    all_polished = []
    for i in range(0, len(text_parts), 3):
        group = text_parts[i:i+3]; group_text = "\n\n".join([f"[Часть {pn}]\n{txt}" for pn, txt in group])
        try:
            completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Ты — летописец."}, {"role": "user", "content": f"{group_text}\n\nСДЕЛАЙ КРАСИВЫЙ РАССКАЗ:"}], temperature=0.5, max_tokens=4000)
            all_polished.append(completion.choices[0].message.content.strip()); time.sleep(2)
        except: all_polished.append(group_text)
    return "\n\n---\n\n".join(all_polished) if all_polished else None

def extract_lore_from_story(story_name):
    parts = get_story_parts(story_name)
    if not parts: return None
    all_text = " ".join([c for _, c, ct, _ in parts if ct == 'text'])
    try:
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "system", "content": "Проанализируй текст и найди термины."}, {"role": "user", "content": f"История: {story_name}\n\nТекст:\n{all_text[:15000]}\n\nНайди все важные термины:"}], temperature=0.3, max_tokens=2000)
        return completion.choices[0].message.content.strip()
    except: return None

def dialogue_learn_step(uid, answer):
    if uid not in dialogue_learning:
        dialogue_learning[uid] = {'step': 'name'}
        return "📝 <b>Диалоговое обучение</b>\n\nО ком или о чём хочешь рассказать? (напиши имя)"
    step = dialogue_learning[uid]['step']
    if step == 'name': dialogue_learning[uid]['keyword'] = clean_text(answer).lower(); dialogue_learning[uid]['step'] = 'description'; return f"📝 Кто такой/что такое <b>{answer}</b>? Опиши подробнее..."
    elif step == 'description': dialogue_learning[uid]['desc'] = answer; dialogue_learning[uid]['category'] = ai_categorize_keyword(dialogue_learning[uid]['keyword'], answer); dialogue_learning[uid]['step'] = 'photo'; return f"📸 Отправь фото (или напиши /skip)\n\n🤖 ИИ определил категорию: {CATEGORY_EMOJI.get(dialogue_learning[uid]['category'], '📚')} {dialogue_learning[uid]['category']}"
    elif step == 'photo':
        if answer == '/skip': dialogue_learning[uid]['photo'] = None
        dialogue_learning[uid]['step'] = 'links'; return "🔗 Есть ли у этого связи? Напиши: враг Имярек\nИли /skip"
    elif step == 'links':
        if answer != '/skip':
            parts = answer.strip().split()
            if len(parts) >= 2: add_wiki_link(dialogue_learning[uid]['keyword'], parts[1], parts[0])
        dialogue_learning[uid]['step'] = 'confirm_category'
        return f"🏷 Подтверди категорию: {CATEGORY_EMOJI.get(dialogue_learning[uid]['category'], '📚')} {dialogue_learning[uid]['category']}\n\nИли напиши другую"
    elif step == 'confirm_category':
        d = dialogue_learning[uid]
        if answer.strip().lower() in CATEGORY_EMOJI: d['category'] = answer.strip().lower()
        execute_db("INSERT OR REPLACE INTO wiki (keyword, description, photo_id, category) VALUES (?, ?, ?, ?)", (d['keyword'], d['desc'], d.get('photo'), d['category']))
        del dialogue_learning[uid]
        return f"✅ Знание «{d['keyword']}» сохранено в категории «{d['category']}»!"

print(">>> Модуль 2 загружен (вспомогательные функции)")
# ============================================================
# ИИ-ФУНКЦИИ ДЛЯ РП-СЕССИЙ
# ============================================================

def ai_gm_suggest(gm_id, chat_id):
    """ИИ предлагает 3 варианта развития сюжета на основе контекста"""
    if chat_id not in rp_sessions or not rp_sessions[chat_id]['active']:
        return None
    
    session = rp_sessions[chat_id]
    recent_context = session['context'][-15:] if len(session['context']) > 15 else session['context']
    context_text = "\n".join([
        f"{'🎭 ГМ' if ctx['user_id'] == 'gm' else '👤 Игрок'}: {ctx['text'][:200]}"
        for ctx in recent_context
    ])
    
    session_context = session.get('session_context', 'Фэнтези мир')
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — ИИ-помощник Гейм-Мастера. Предложи 3 ВАРИАНТА развития сюжета.
Каждый вариант должен быть КРАТКИМ (1-2 предложения) и создавать ИНТРИГУ.
Используй классические тропы: неожиданный поворот, появление NPC, опасность, загадку, моральный выбор.
Формат ответа:
1. 🔥 [Вариант 1]
2. 🌪 [Вариант 2]  
3. 💀 [Вариант 3]"""
            }, {
                "role": "user",
                "content": f"МИР: {session_context}\n\nПОСЛЕДНИЕ СОБЫТИЯ:\n{context_text}\n\nПредложи 3 варианта развития сюжета:"
            }],
            temperature=0.8,
            max_tokens=400
        )
        suggestions = completion.choices[0].message.content.strip()
        
        bot.send_message(
            gm_id,
            f"🎲 <b>AI-Советник:</b>\n\n{suggestions}\n\n<i>Выбери направление или используй как вдохновение</i>",
            parse_mode="HTML"
        )
        return suggestions
    except Exception as e:
        print(f"Ошибка AI GM: {e}")
        return None


def generate_npc(npc_type="случайный", context=""):
    """Генерирует NPC с уникальным характером и предысторией"""
    
    archetypes = {
        "торговец": "хитрый торговец с тёмным прошлым",
        "стражник": "уставший стражник, который видел слишком много",
        "маг": "эксцентричный маг, одержимый запретными знаниями",
        "бармен": "бармен, который знает все слухи в городе",
        "наёмник": "наёмник с кодексом чести и тёмной тайной",
        "жрец": "жрец, потерявший веру но скрывающий это",
        "вор": "благородный вор, грабящий только богатых",
        "учёный": "безумный учёный на грани великого открытия",
        "кузнец": "мастер-оружейник, хранящий секрет легендарного металла",
        "шпион": "двойной агент, не помнящий на чьей он стороне",
        "целитель": "травница с даром, за который её преследуют",
        "случайный": "уникальный персонаж со сложной судьбой"
    }
    
    archetype = archetypes.get(npc_type, npc_type)
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — генератор NPC для RPG. Создай запоминающегося персонажа.
Формат ответа (строго соблюдай):
🎭 ИМЯ: [имя]
📋 АРХЕТИП: [архетип]
💬 МАНЕРА РЕЧИ: [2-3 характерные фразы или особенности речи]
🎯 МОТИВАЦИЯ: [чего хочет персонаж]
🔒 ТАЙНА: [что скрывает]
⚔️ ОСОБЫЕ ЧЕРТЫ: [2-3 уникальные особенности]
📖 КВЕСТ-КРЮЧОК: [как NPC может вовлечь игроков в приключение]"""
            }, {
                "role": "user",
                "content": f"Создай NPC: {archetype}\nКонтекст мира: {context[:500]}"
            }],
            temperature=0.9,
            max_tokens=500
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_location(location_type="таверна", mood="мрачная"):
    """Генерирует детальное описание локации с элементами взаимодействия"""
    
    location_prompts = {
        "таверна": "таверна в фэнтези-мире, опиши завсегдатаев, особые напитки, тёмные углы",
        "лес": "древний лес полный магии и опасностей, опиши флору, звуки, скрытые тропы",
        "замок": "заброшенный замок с призраками прошлого, опиши архитектуру, ловушки, сокровища",
        "рынок": "шумный восточный базар, опиши торговцев, редкие товары, карманников",
        "подземелье": "тёмное подземелье с древними рунами, опиши опасности, головоломки, обитателей",
        "храм": "забытый храм забытого бога, опиши алтари, проклятия, благословения",
        "библиотека": "древняя библиотека с запретными фолиантами",
        "болото": "ядовитые топи, где обитают древние твари",
        "горы": "заснеженные пики с пещерами ледяных драконов",
        "порт": "пиратская гавань с контрабандистами и морскими легендами"
    }
    
    desc = location_prompts.get(location_type, location_type)
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — генератор локаций для RPG. Создай АТМОСФЕРНОЕ описание.
Формат ответа:
🏛 НАЗВАНИЕ: [название локации]
👁 ПЕРВЫЙ ВЗГЛЯД: [что видят игроки входя]
👃 ЗАПАХИ И ЗВУКИ: [сенсорное описание]
🔍 ИНТЕРЕСНЫЕ ДЕТАЛИ: [3-4 элемента для исследования]
⚠️ ОПАСНОСТИ: [скрытые угрозы]
💎 НАГРАДЫ: [что можно найти]
📜 СЛУХИ: [местная легенда или сплетня]"""
            }, {
                "role": "user",
                "content": f"Создай локацию: {desc}\nНастроение: {mood}"
            }],
            temperature=0.8,
            max_tokens=600
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_random_encounter(environment="лес", party_level="средний", time_of_day="ночь"):
    """Генерирует случайную встречу с учётом контекста"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — генератор случайных встреч для RPG. Создай НЕОЖИДАННОЕ событие.
Оно должно быть НЕ БОЕВЫМ (или с возможностью избежать боя).
Формат ответа:
🎲 СОБЫТИЕ: [название]
📖 ОПИСАНИЕ: [2-3 предложения]
👥 УЧАСТНИКИ: [кто вовлечён]
⚡ ВАРИАНТЫ ДЕЙСТВИЙ:
  A) [первый вариант]
  B) [второй вариант]  
  C) [третий вариант]
🎁 ПОСЛЕДСТВИЯ: [что случится в зависимости от выбора]"""
            }, {
                "role": "user",
                "content": f"Создай случайную встречу:\nМестность: {environment}\nУровень группы: {party_level}\nВремя суток: {time_of_day}"
            }],
            temperature=0.9,
            max_tokens=500
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def ai_oracle_interpret(dice_result, action_description, context=""):
    """ИИ интерпретирует результат броска и создаёт нарративное описание"""
    
    result_type = "критический успех" if dice_result >= 20 else "успех" if dice_result >= 15 else "частичный успех" if dice_result >= 10 else "провал" if dice_result >= 5 else "критический провал"
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": f"""Ты — Оракул в RPG. Игрок выбросил {dice_result} (это {result_type}) при попытке: "{action_description}".
Опиши нарративно что происходит. Будь драматичным и кинематографичным.
При критическом успехе — добавь неожиданный бонус.
При критическом провале — добавь интересное осложнение (не просто "не получилось").
ОТВЕТЬ В 2-3 ПРЕДЛОЖЕНИЯХ, от лица рассказчика."""
            }, {
                "role": "user",
                "content": f"Действие: {action_description}\nРезультат броска: {dice_result} ({result_type})\nКонтекст: {context[:300]}"
            }],
            temperature=0.7,
            max_tokens=300
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_puzzle(difficulty="средняя", theme="магия"):
    """Генерирует загадку с тремя уровнями подсказок"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — создатель загадок для RPG. Придумай УНИКАЛЬНУЮ загадку.
Формат ответа:
🧩 ЗАГАДКА: [текст загадки или описание головоломки]
🎯 ОТВЕТ: [правильный ответ]
💡 ПОДСКАЗКА 1 (лёгкая): [общая подсказка]
💡 ПОДСКАЗКА 2 (средняя): [более прямая подсказка]  
💡 ПОДСКАЗКА 3 (почти ответ): [почти раскрывает ответ]
🔮 ПОСЛЕДСТВИЯ:
  ✅ Успех: [что получат игроки]
  ❌ Провал: [что случится при ошибке]"""
            }, {
                "role": "user",
                "content": f"Создай загадку:\nСложность: {difficulty}\nТема: {theme}"
            }],
            temperature=0.9,
            max_tokens=500
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_prophecy(style="туманное", elements=["огонь", "корона", "падение"]):
    """Генерирует пророчество в выбранном стиле"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": f"""Ты — Древний Оракул. Создай ПРОРОЧЕСТВО.
Стиль: {style}
Включи элементы: {', '.join(elements)}
Пророчество должно быть:
- Туманным и допускающим множественные толкования
- Иметь зловещий или предостерегающий тон
- Содержать скрытый смысл, понятный ГМ-у

Формат ответа:
🔮 ПРОРОЧЕСТВО: [текст пророчества в 2-4 строки]
📖 РАСШИФРОВКА ДЛЯ ГМ-а: [что на самом деле значит пророчество]
🎭 КАК ОБЫГРАТЬ: [3 идеи как вплести в сюжет]"""
            }, {
                "role": "user",
                "content": f"Создай пророчество в стиле: {style}\nКлючевые элементы: {', '.join(elements)}"
            }],
            temperature=0.9,
            max_tokens=400
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_npc_dialogue(npc_name, npc_personality, player_question, context=""):
    """Генерирует ответ NPC в соответствии с его характером"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": f"""Ты — {npc_name}. Твоя личность: {npc_personality}
Отвечай В ХАРАКТЕРЕ. Учитывай свою мотивацию, манеру речи, тайны.
Не говори того, что не знаешь. Можешь лгать, если это в твоём характере.
Используй речевые особенности, акцент, характерные фразы.
ОТВЕТЬ В 1-3 ПРЕДЛОЖЕНИЯХ."""
            }, {
                "role": "user",
                "content": f"Контекст: {context[:500]}\n\nИгрок спрашивает: {player_question}\n\nОтветь как {npc_name}:"
            }],
            temperature=0.8,
            max_tokens=300
        )
        return completion.choices[0].message.content.strip()
    except:
        return None


def generate_quest(quest_type="основной", difficulty="средний", context=""):
    """Генерирует квест с несколькими этапами и вариативностью"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "system",
                "content": """Ты — генератор квестов для RPG. Создай увлекательный квест.
Формат ответа:
⚔️ НАЗВАНИЕ: [название квеста]
📜 ОПИСАНИЕ: [2-3 предложения завязки]
👤 ЗАКАЗЧИК: [кто даёт квест и почему]
🎯 ЦЕЛЬ: [что нужно сделать]
🗺 ЭТАПЫ:
  1) [первый этап]
  2) [второй этап]
  3) [третий этап]
⚡ РАЗВИЛКА: [моральный выбор или неожиданный поворот]
💎 НАГРАДА: [что получат игроки]
☠️ ОСЛОЖНЕНИЕ: [что может пойти не так]"""
            }, {
                "role": "user",
                "content": f"Создай {quest_type} квест сложности {difficulty}\nКонтекст мира: {context[:500]}"
            }],
            temperature=0.8,
            max_tokens=600
        )
        return completion.choices[0].message.content.strip()
    except:
        return None

print(">>> Модуль 3 загружен (ИИ-функции РП)")

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_kb(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("👤 Мой профиль", "📚 База знаний")
    markup.add("🛒 Магазин", "🎒 Инвентарь")
    markup.add("🏆 Топ игроков", "🔖 Закладки")
    if uid == ADMIN_ID:
        markup.add("🎭 РП-сессия", "📜 Обучить ГМ-а")
        markup.add("📖 Истории", "📢 Рассылка")
        markup.add("🔗 Связи", "📊 Статистика Лора")
        markup.add("🎲 Случайный Лор", "❓ Викторина")
        markup.add("💬 Диалог-обучение", "📥 Импорт из Истории")
        markup.add("🤖 Авто-категоризация", "⚙️ Режим чата")
        markup.add("🔧 Управление БД", "🛠 Админ-панель")
    else:
        markup.add("🆔 Мой ID", "🎁 Ежедневный бонус")
    return markup

def admin_panel_kb():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("💾 Бэкап БД", "📈 Общая статистика")
    markup.add("🚫 Бан-лист", "💰 Выдать валюту")
    markup.add("🏷 Добавить товар", "📤 Экспорт вики")
    markup.add("🔙 Назад")
    return markup

def shop_kb(items):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for item_id, name, price, currency, emoji in items:
        markup.add(types.InlineKeyboardButton(f"{emoji} {name} — {price} {currency}", callback_data=f"buy_{item_id}"))
    return markup

def rp_menu_kb():
    """Клавиатура для меню РП-сессий"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🎭 Начать сессию", "🎭 Остановить сессию")
    markup.add("📖 Повествование", "📋 Контекст чата")
    markup.add("🤖 AI-Советник", "🎲 Оракул")
    markup.add("👤 Генератор NPC", "⚔️ Генератор Квестов")
    markup.add("🏛 Генератор Локаций", "🎲 Случайная Встреча")
    markup.add("🧩 Загадка", "🔮 Пророчество")
    markup.add("💬 Диалог NPC", "🔙 Назад")
    return markup

def categories_kb(categories):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for cat, count in categories:
        markup.add(types.InlineKeyboardButton(f"{CATEGORY_EMOJI.get(cat, '📚')} {cat.capitalize()} ({count})", callback_data=f"cat_{cat}"))
    return markup

def db_management_kb():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔄 Изменить ключ", "🖼 Обновить фото")
    markup.add("🗑 Удалить фото", "📋 Просмотр записи")
    markup.add("🔙 Назад")
    return markup
    # ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================

@bot.message_handler(commands=['start'])
def start_cmd(message):
    uid = message.from_user.id; chat_id = message.chat.id
    init_db()
    execute_db("INSERT OR IGNORE INTO broadcast_users (user_id, chat_id) VALUES (?, ?)", (uid, chat_id))
    ensure_player(uid, message.from_user.first_name)
    bot.send_message(
        chat_id,
        "🕯 <b>Библиотека Хуфы открыта.</b>\n\n"
        "🤖 ИИ-сортировка! Спроси «что ты знаешь»\n"
        "🎭 РП-сессии с AI-помощником\n"
        "📖 Истории, викторины, связи\n"
        "🛒 Магазин, 🎒 инвентарь, 🎁 ежедневный бонус, 🏆 топ игроков\n"
        "🔖 /bookmark ключ — сохранить статью, 🔍 /search запрос — поиск\n"
        "🎲 /roll d20\n"
        "🧠 /gm_suggest /npc /quest /oracle",
        reply_markup=main_kb(uid), parse_mode="HTML"
    )

@bot.message_handler(commands=['create'])
def create_cmd(message):
    uid = message.from_user.id; chat_id = message.chat.id
    user_states[uid] = 'reg_name'
    bot.send_message(chat_id, "🎭 Как зовут героя?")

@bot.message_handler(commands=['roll'])
def handle_roll_cmd(message):
    args = message.text.replace('/roll', '').strip() or 'd20'
    match = re.match(r'(\d+)?d(\d+)([+-]\d+)?', args)
    if not match: bot.reply_to(message, "❌ Формат: /roll d20+5"); return
    count = int(match.group(1) or 1); dice = int(match.group(2)); modifier = int(match.group(3) or 0)
    rolls = [random.randint(1, dice) for _ in range(count)]
    total = sum(rolls) + modifier
    response = f"🎲 <b>{args}:</b> {total}"
    if dice == 20:
        if rolls[0] == 20: response += " ✨КРИТ!"
        elif rolls[0] == 1: response += " 💀ПРОВАЛ!"
    bot.send_message(message.chat.id, response, parse_mode="HTML")

@bot.message_handler(commands=['getid'])
def get_my_id(message):
    bot.reply_to(message, f"🆔 Ваш ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(commands=['users'])
def list_users_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    players = execute_db("SELECT user_id, name FROM players", (), True)
    if not players: bot.reply_to(message, "📭 Нет игроков!"); return
    response = "👥 <b>Игроки:</b>\n\n" + "\n".join([f"• {name} — <code>{uid}</code>" for uid, name in players])
    bot.send_message(message.from_user.id, response, parse_mode="HTML")

@bot.message_handler(commands=['rp_start'])
def rp_start_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    chat_id = message.chat.id
    thread_id = message.message_thread_id if hasattr(message, 'message_thread_id') else None
    existing_context = get_chat_context(chat_id)
    if existing_context:
        user_states[uid] = 'rp_name'
        temp_learning[uid] = {'rp_chat_id': chat_id, 'thread_id': thread_id, 'context': existing_context}
        bot.send_message(uid, f"📖 <b>Найден контекст:</b>\n{existing_context[:600]}\n\n🎭 Введи название сессии (или /skip для авто-названия):")
    else:
        user_states[uid] = 'rp_context'
        temp_learning[uid] = {'rp_chat_id': chat_id, 'thread_id': thread_id}
        bot.send_message(uid, "📖 Контекст не найден.\n\nО чём будет сессия? Опиши тему (я поищу информацию в интернете):")

@bot.message_handler(commands=['rp_narrate'])
def rp_narrate_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    text = message.text.replace('/rp_narrate', '').strip()
    if not text: bot.reply_to(message, "❌ Напиши текст: /rp_narrate [текст]"); return
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat: gm_narrate(active_chat, text); bot.reply_to(message, "✅ Отправлено!")
    else: bot.reply_to(message, "❌ Нет активных сессий!")

@bot.message_handler(commands=['rp_stop'])
def rp_stop_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat:
        thread_id = rp_sessions[active_chat].get('thread_id')
        stop_rp_session(active_chat)
        bot.send_message(active_chat, "🎭 <b>РП-сессия завершена.</b>", parse_mode="HTML", message_thread_id=thread_id)
        bot.reply_to(message, "✅ Сессия остановлена!")
    else: bot.reply_to(message, "❌ Нет активных сессий!")

@bot.message_handler(commands=['rp_mode'])
def rp_mode_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 2:
        current = execute_db("SELECT mode FROM rp_channels WHERE chat_id = ?", (chat_id,), True)
        mode = current[0][0] if current else 'silent'
        bot.reply_to(message, f"⚙️ Режим: <b>{mode}</b>\n• active — бот в РП\n• silent — бот молчит\n• answer — отвечает", parse_mode="HTML")
        return
    mode = args[1].lower()
    if mode in ['active', 'silent', 'answer']:
        execute_db("INSERT OR REPLACE INTO rp_channels (chat_id, mode) VALUES (?, ?)", (chat_id, mode))
        bot.reply_to(message, f"✅ Режим изменён на: <b>{mode}</b>", parse_mode="HTML")

# ============================================================
# КОМАНДЫ ИИ-ИНСТРУМЕНТОВ ДЛЯ РП
# ============================================================

@bot.message_handler(commands=['gm_suggest'])
def gm_suggest_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat:
        bot.send_message(uid, "🤔 <i>ИИ анализирует ситуацию...</i>", parse_mode="HTML")
        ai_gm_suggest(uid, active_chat)
    else:
        bot.reply_to(message, "❌ Нет активных сессий!")

@bot.message_handler(commands=['npc'])
def npc_generator_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    args = message.text.replace('/npc', '').strip()
    npc_type = args if args else "случайный"
    status_msg = bot.send_message(uid, f"🎭 <i>Генерирую {npc_type}...</i>", parse_mode="HTML")
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    npc = generate_npc(npc_type, context)
    bot.delete_message(uid, status_msg.message_id)
    if npc:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🎭 Ввести в игру", callback_data=f"npc_play_{uid}"),
            types.InlineKeyboardButton("🔄 Сгенерировать ещё", callback_data=f"npc_reroll_{npc_type}")
        )
        bot.send_message(uid, npc, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(uid, "⚠️ Ошибка генерации NPC")

@bot.message_handler(commands=['location'])
def location_generator_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    args = message.text.replace('/location', '').strip()
    parts = args.split('|')
    loc_type = parts[0].strip() if parts and parts[0].strip() else "таверна"
    mood = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "загадочная"
    status_msg = bot.send_message(uid, f"🏛 <i>Создаю {loc_type}...</i>", parse_mode="HTML")
    location = generate_location(loc_type, mood)
    bot.delete_message(uid, status_msg.message_id)
    if location:
        bot.send_message(uid, location, parse_mode="HTML")
    else:
        bot.send_message(uid, "⚠️ Ошибка генерации локации")

@bot.message_handler(commands=['encounter'])
def encounter_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    args = message.text.replace('/encounter', '').strip() or "лес|средний|день"
    parts = args.split('|')
    encounter = generate_random_encounter(
        environment=parts[0].strip() if len(parts) > 0 else "лес",
        party_level=parts[1].strip() if len(parts) > 1 else "средний",
        time_of_day=parts[2].strip() if len(parts) > 2 else "день"
    )
    if encounter:
        if active_chat:
            gm_narrate(active_chat, f"🎲 <b>Случайная встреча:</b>\n\n{encounter}")
            bot.send_message(uid, "✅ Событие отправлено в чат!")
        else:
            bot.send_message(uid, encounter, parse_mode="HTML")
    else:
        bot.send_message(uid, "⚠️ Ошибка генерации события")

@bot.message_handler(commands=['oracle'])
def oracle_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    text = message.text.replace('/oracle', '').strip()
    if not text: bot.reply_to(message, "🎲 Использование: /oracle [описание действия]"); return
    roll = random.randint(1, 20)
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    interpretation = ai_oracle_interpret(roll, text, context)
    if interpretation:
        crit_emoji = "✨" if roll == 20 else "💀" if roll == 1 else ""
        response = f"🎲 <b>Оракул:</b> [d20 = {roll}] {crit_emoji}\n\n📖 {interpretation}"
        if active_chat:
            gm_narrate(active_chat, response)
            bot.send_message(uid, "✅ Отправлено в чат!")
        else:
            bot.send_message(uid, response, parse_mode="HTML")

@bot.message_handler(commands=['puzzle'])
def puzzle_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    args = message.text.replace('/puzzle', '').strip()
    parts = args.split('|')
    difficulty = parts[0].strip() if parts and parts[0].strip() else "средняя"
    theme = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "магия"
    puzzle = generate_puzzle(difficulty, theme)
    if puzzle:
        quiz_data[f"puzzle_{uid}"] = {'puzzle': puzzle, 'hints_shown': 0}
        puzzle_parts = puzzle.split('🎯 ОТВЕТ:')
        display_text = puzzle_parts[0] + "\n\n<i>Используй кнопки для подсказок</i>"
        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(
            types.InlineKeyboardButton("💡 Подсказка 1", callback_data=f"hint_1_{uid}"),
            types.InlineKeyboardButton("💡 Подсказка 2", callback_data=f"hint_2_{uid}"),
            types.InlineKeyboardButton("💡 Подсказка 3", callback_data=f"hint_3_{uid}")
        )
        bot.send_message(uid, display_text, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(uid, "⚠️ Ошибка генерации загадки")

@bot.message_handler(commands=['prophecy'])
def prophecy_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    args = message.text.replace('/prophecy', '').strip()
    elements = args.split(',') if args else ["кровь", "луна", "возвращение"]
    elements = [e.strip() for e in elements if e.strip()]
    styles = ["туманное", "зловещее", "эпическое", "загадочное", "обнадёживающее"]
    style = random.choice(styles)
    prophecy = generate_prophecy(style, elements)
    if prophecy:
        parts = prophecy.split('📖 РАСШИФРОВКА ДЛЯ ГМ-а:')
        player_part = parts[0]
        gm_part = parts[1] if len(parts) > 1 else ""
        active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
        if active_chat:
            gm_narrate(active_chat, f"🔮 <b>Древнее пророчество:</b>\n\n{player_part}")
            if gm_part:
                bot.send_message(uid, f"📖 <b>Расшифровка для ГМ-а:</b>\n{gm_part}", parse_mode="HTML")
            bot.send_message(uid, "✅ Пророчество отправлено в чат!")
        else:
            bot.send_message(uid, prophecy, parse_mode="HTML")

@bot.message_handler(commands=['dialogue'])
def dialogue_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    text = message.text.replace('/dialogue', '').strip()
    parts = text.split('|')
    if len(parts) < 3:
        bot.reply_to(message, "📝 Использование: /dialogue [имя NPC] | [характер] | [вопрос игрока]")
        return
    npc_name = parts[0].strip()
    npc_personality = parts[1].strip()
    player_question = parts[2].strip()
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    response = generate_npc_dialogue(npc_name, npc_personality, player_question, context)
    if response:
        if active_chat:
            gm_narrate(active_chat, f"💬 <b>{npc_name}:</b> {response}")
            bot.send_message(uid, "✅ Ответ отправлен в чат!")
        else:
            bot.send_message(uid, f"💬 <b>{npc_name}:</b>\n{response}", parse_mode="HTML")

@bot.message_handler(commands=['quest'])
def quest_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    args = message.text.replace('/quest', '').strip()
    parts = args.split('|')
    quest_type = parts[0].strip() if parts and parts[0].strip() else "основной"
    difficulty = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "средний"
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    quest = generate_quest(quest_type, difficulty, context)
    if quest:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📜 Дать квест игрокам", callback_data=f"quest_give_{uid}"),
            types.InlineKeyboardButton("🔄 Другой квест", callback_data=f"quest_reroll_{quest_type}_{difficulty}")
        )
        bot.send_message(uid, quest, reply_markup=markup, parse_mode="HTML")

# ============================================================
# ОБРАБОТЧИКИ КНОПОК МЕНЮ
# ============================================================

@bot.message_handler(func=lambda m: m.text == "👤 Мой профиль")
def show_profile(message):
    caption, photo = format_profile(message.from_user.id)
    if not caption: bot.reply_to(message, "Создай героя: /create"); return
    safe_send(message.chat.id, caption, photo)

@bot.message_handler(func=lambda m: m.text == "🆔 Мой ID")
def show_my_id(message):
    bot.reply_to(message, f"🆔 <b>Ваш ID:</b> <code>{message.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🎭 РП-сессия" and m.from_user.id == ADMIN_ID)
def rp_menu(message):
    bot.send_message(message.from_user.id, "🎭 <b>Меню РП-сессий</b>\n\nВыбери инструмент:", reply_markup=rp_menu_kb(), parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🎭 Начать сессию" and m.from_user.id == ADMIN_ID)
def rp_start_btn(message):
    uid = message.from_user.id; chat_id = message.chat.id
    thread_id = message.message_thread_id if hasattr(message, 'message_thread_id') else None
    existing_context = get_chat_context(chat_id)
    if existing_context:
        user_states[uid] = 'rp_name'
        temp_learning[uid] = {'rp_chat_id': chat_id, 'thread_id': thread_id, 'context': existing_context}
        bot.send_message(uid, f"📖 <b>Найден контекст:</b>\n{existing_context[:600]}\n\n🎭 Введи название сессии (или /skip для авто-названия):")
    else:
        user_states[uid] = 'rp_context'
        temp_learning[uid] = {'rp_chat_id': chat_id, 'thread_id': thread_id}
        bot.send_message(uid, "📖 Контекст не найден.\n\nО чём будет сессия? Опиши тему (я поищу информацию в интернете):")

@bot.message_handler(func=lambda m: m.text == "🎭 Остановить сессию" and m.from_user.id == ADMIN_ID)
def rp_stop_btn(message):
    uid = message.from_user.id
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat:
        thread_id = rp_sessions[active_chat].get('thread_id')
        stop_rp_session(active_chat)
        bot.send_message(active_chat, "🎭 <b>РП-сессия завершена.</b>", parse_mode="HTML", message_thread_id=thread_id)
        bot.send_message(uid, "✅ Сессия остановлена!", reply_markup=main_kb(uid))
    else: bot.send_message(uid, "❌ Нет активных сессий!", reply_markup=main_kb(uid))

@bot.message_handler(func=lambda m: m.text == "📖 Повествование" and m.from_user.id == ADMIN_ID)
def rp_narrate_btn(message):
    uid = message.from_user.id
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if not active_chat: bot.send_message(uid, "❌ Нет активных сессий!"); return
    user_states[uid] = 'rp_narrate_text'
    bot.send_message(uid, "📖 Введи текст повествования (/cancel):")

@bot.message_handler(func=lambda m: m.text == "📋 Контекст чата" and m.from_user.id == ADMIN_ID)
def show_context_btn(message):
    uid = message.from_user.id; chat_id = message.chat.id
    context = get_chat_context(chat_id)
    if context: bot.send_message(uid, f"📖 <b>Контекст чата:</b>\n\n{context}", parse_mode="HTML")
    else:
        user_states[uid] = 'rp_context'
        temp_learning[uid] = {'rp_chat_id': chat_id, 'thread_id': None}
        bot.send_message(uid, "📖 Контекст не найден.\n\nО чём будет сессия? Опиши тему:")

@bot.message_handler(func=lambda m: m.text == "⚙️ Режим чата" and m.from_user.id == ADMIN_ID)
def rp_mode_menu(message):
    chat_id = message.chat.id
    current = execute_db("SELECT mode FROM rp_channels WHERE chat_id = ?", (chat_id,), True)
    mode = current[0][0] if current else 'silent'
    markup = types.InlineKeyboardMarkup(row_width=3)
    for m in ['active', 'silent', 'answer']:
        icon = "✅ " if m == mode else ""
        markup.add(types.InlineKeyboardButton(f"{icon}{m}", callback_data=f"rpmode_{m}"))
    bot.send_message(chat_id, f"⚙️ Режим: <b>{mode}</b>", reply_markup=markup, parse_mode="HTML")

# ============================================================
# ОБРАБОТЧИКИ КНОПОК ИИ-ИНСТРУМЕНТОВ
# ============================================================

@bot.message_handler(func=lambda m: m.text in [
    "🤖 AI-Советник", "🎲 Оракул", "👤 Генератор NPC",
    "⚔️ Генератор Квестов", "🏛 Генератор Локаций",
    "🎲 Случайная Встреча", "🧩 Загадка", "🔮 Пророчество",
    "💬 Диалог NPC"
] and m.from_user.id == ADMIN_ID)
def rp_ai_tools(message):
    uid = message.from_user.id
    
    tools_map = {
        "🤖 AI-Советник": ("gm_suggest", "Получить 3 варианта развития сюжета от ИИ"),
        "🎲 Оракул": ("oracle", "Бросок d20 + нарративная интерпретация"),
        "👤 Генератор NPC": ("npc", "Создать персонажа: /npc [тип]"),
        "⚔️ Генератор Квестов": ("quest", "Создать квест: /quest [тип] | [сложность]"),
        "🏛 Генератор Локаций": ("location", "Создать локацию: /location [тип] | [настроение]"),
        "🎲 Случайная Встреча": ("encounter", "Случайное событие: /encounter [местность] | [уровень] | [время]"),
        "🧩 Загадка": ("puzzle", "Создать загадку: /puzzle [сложность] | [тема]"),
        "🔮 Пророчество": ("prophecy", "Создать пророчество: /prophecy [элемент1, элемент2]"),
        "💬 Диалог NPC": ("dialogue", "Ответ NPC: /dialogue [NPC] | [характер] | [вопрос]")
    }
    
    if message.text in tools_map:
        cmd, help_text = tools_map[message.text]
        bot.send_message(uid, f"📝 <b>{message.text}</b>\n\n{help_text}\n\nИспользуй команду /{cmd}", parse_mode="HTML")

print(">>> Модуль 4 загружен (клавиатуры и обработчики)")
# ============================================================
# CALLBACK ОБРАБОТЧИКИ
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('rpmode_'))
def handle_rp_mode_callback(call):
    if call.from_user.id != ADMIN_ID: return
    execute_db("INSERT OR REPLACE INTO rp_channels (chat_id, mode) VALUES (?, ?)", (call.message.chat.id, call.data[7:]))
    bot.edit_message_text(f"✅ Режим: <b>{call.data[7:]}</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cat_'))
def handle_category_callback(call):
    entries = get_wiki_by_category(call.data[4:])
    if not entries: bot.answer_callback_query(call.id, "Пусто"); return
    emoji = CATEGORY_EMOJI.get(call.data[4:], '📚')
    text = f"{emoji} <b>Категория: {call.data[4:].capitalize()}</b>\n\n"
    for i, (kw, desc, _) in enumerate(entries, 1):
        part = f"{i}. <b>{kw.capitalize()}</b>\n   {desc[:100]}{'...' if len(desc) > 100 else ''}\n\n"
        if len(text + part) > 3500:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML")
            text = part
        else: text += part
    if text: bot.send_message(call.message.chat.id, text, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('quiz_'))
def handle_quiz_callback(call):
    parts = call.data.split('_')
    quiz = quiz_data.get(int(parts[1]))
    if not quiz: bot.answer_callback_query(call.id, "⏰ Устарела!"); return
    if quiz['options'][int(parts[2])] == parts[3]:
        bot.answer_callback_query(call.id, "✅ Правильно!")
        execute_db("UPDATE players SET хуфа = хуфа + 10 WHERE user_id = ?", (call.from_user.id,))
    else: bot.answer_callback_query(call.id, f"❌ Ответ: {parts[3].capitalize()}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('hint_'))
def handle_hint_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    parts = call.data.split('_')
    hint_level = int(parts[1])
    original_uid = int(parts[2])
    
    puzzle_data_key = f"puzzle_{original_uid}"
    puzzle_entry = quiz_data.get(puzzle_data_key, {})
    puzzle = puzzle_entry.get('puzzle', '') if isinstance(puzzle_entry, dict) else ''
    
    if not puzzle:
        bot.answer_callback_query(call.id, "⏰ Загадка устарела")
        return
    
    hint_marker = f"💡 ПОДСКАЗКА {hint_level}"
    answer_marker = "🎯 ОТВЕТ:"
    
    if hint_marker in puzzle:
        hint_start = puzzle.find(hint_marker)
        next_hint = puzzle.find("💡 ПОДСКАЗКА", hint_start + 1)
        if next_hint == -1:
            next_hint = puzzle.find(answer_marker, hint_start)
        
        hint = puzzle[hint_start:next_hint].strip() if next_hint != -1 else puzzle[hint_start:].strip()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🎯 Показать ответ", callback_data=f"answer_{original_uid}"))
        
        bot.send_message(uid, hint, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "Подсказка отправлена!")

@bot.callback_query_handler(func=lambda call: call.data.startswith('answer_'))
def handle_answer_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    original_uid = int(call.data.split('_')[1])
    puzzle_data_key = f"puzzle_{original_uid}"
    puzzle_entry = quiz_data.get(puzzle_data_key, {})
    puzzle = puzzle_entry.get('puzzle', '') if isinstance(puzzle_entry, dict) else ''
    
    if not puzzle:
        bot.answer_callback_query(call.id, "⏰ Загадка устарела")
        return
    
    answer_marker = "🎯 ОТВЕТ:"
    consequences_marker = "🔮 ПОСЛЕДСТВИЯ:"
    
    if answer_marker in puzzle:
        answer_start = puzzle.find(answer_marker)
        answer_end = puzzle.find(consequences_marker, answer_start) if consequences_marker in puzzle else len(puzzle)
        answer_text = puzzle[answer_start:answer_end].strip()
        bot.send_message(uid, f"📖 <b>Ответ на загадку:</b>\n\n{answer_text}", parse_mode="HTML")
    
    bot.answer_callback_query(call.id, "Ответ отправлен!")

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_play_'))
def handle_npc_play_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat:
        original_text = call.message.text or call.message.caption or ""
        gm_narrate(active_chat, f"🎭 <b>Новый NPC появился:</b>\n\n{original_text}")
        bot.send_message(uid, "✅ NPC введён в игру!")
        bot.answer_callback_query(call.id, "NPC в игре!")
    else:
        bot.answer_callback_query(call.id, "Нет активной сессии!")

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_reroll_'))
def handle_npc_reroll_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    npc_type = call.data.replace('npc_reroll_', '')
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    
    npc = generate_npc(npc_type, context)
    if npc:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🎭 Ввести в игру", callback_data=f"npc_play_{uid}"),
            types.InlineKeyboardButton("🔄 Сгенерировать ещё", callback_data=f"npc_reroll_{npc_type}")
        )
        bot.edit_message_text(npc, uid, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "Новый NPC!")
    else:
        bot.answer_callback_query(call.id, "Ошибка генерации")

@bot.callback_query_handler(func=lambda call: call.data.startswith('quest_give_'))
def handle_quest_give_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if active_chat:
        original_text = call.message.text or call.message.caption or ""
        gm_narrate(active_chat, f"⚔️ <b>Новое задание!</b>\n\n{original_text}")
        bot.send_message(uid, "✅ Квест выдан игрокам!")
        bot.answer_callback_query(call.id, "Квест в игре!")
    else:
        bot.answer_callback_query(call.id, "Нет активной сессии!")

@bot.callback_query_handler(func=lambda call: call.data.startswith('quest_reroll_'))
def handle_quest_reroll_callback(call):
    uid = call.from_user.id
    if uid != ADMIN_ID: return
    
    parts = call.data.split('_')
    quest_type = parts[2] if len(parts) > 2 else "основной"
    difficulty = parts[3] if len(parts) > 3 else "средний"
    
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    context = rp_sessions[active_chat].get('session_context', '') if active_chat else ''
    
    quest = generate_quest(quest_type, difficulty, context)
    if quest:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📜 Дать квест игрокам", callback_data=f"quest_give_{uid}"),
            types.InlineKeyboardButton("🔄 Другой квест", callback_data=f"quest_reroll_{quest_type}_{difficulty}")
        )
        bot.edit_message_text(quest, uid, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "Новый квест!")
    else:
        bot.answer_callback_query(call.id, "Ошибка генерации")


# ============================================================
# НОВЫЕ ФУНКЦИИ: ЭКОНОМИКА (магазин/инвентарь/бонус/топ)
# ============================================================

def ensure_player(uid, name="Безымянный"):
    if not execute_db("SELECT 1 FROM players WHERE user_id = ?", (uid,), True):
        execute_db("INSERT INTO players (user_id, name, bio, photo) VALUES (?, ?, ?, ?)", (uid, name, "", None))

@bot.message_handler(func=lambda m: m.text == "🛒 Магазин")
def shop_cmd(message):
    items = execute_db("SELECT id, name, price, currency, emoji FROM items ORDER BY price", (), True)
    if not items:
        bot.send_message(message.chat.id, "🛒 Магазин пока пуст. Загляни позже!")
        return
    bot.send_message(message.chat.id, "🛒 <b>Магазин</b>\n\nВыбери товар:", reply_markup=shop_kb(items), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy_callback(call):
    uid = call.from_user.id
    item_id = int(call.data.split('_')[1])
    item = execute_db("SELECT id, name, price, currency, emoji FROM items WHERE id = ?", (item_id,), True)
    if not item:
        bot.answer_callback_query(call.id, "❌ Товар не найден"); return
    item_id, name, price, currency, emoji = item[0]
    ensure_player(uid, call.from_user.first_name)
    balance = execute_db(f"SELECT {currency} FROM players WHERE user_id = ?", (uid,), True)
    current = balance[0][0] if balance else 0
    if current < price:
        bot.answer_callback_query(call.id, f"❌ Не хватает {currency}! Нужно {price}, у тебя {current}", show_alert=True)
        return
    execute_db(f"UPDATE players SET {currency} = {currency} - ? WHERE user_id = ?", (price, uid))
    existing = execute_db("SELECT qty FROM inventory WHERE user_id = ? AND item_id = ?", (uid, item_id), True)
    if existing:
        execute_db("UPDATE inventory SET qty = qty + 1 WHERE user_id = ? AND item_id = ?", (uid, item_id))
    else:
        execute_db("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)", (uid, item_id))
    bot.answer_callback_query(call.id, f"✅ Куплено: {emoji} {name}!", show_alert=True)

@bot.message_handler(func=lambda m: m.text == "🎒 Инвентарь")
def inventory_cmd(message):
    uid = message.from_user.id
    rows = execute_db("""SELECT items.name, items.emoji, inventory.qty FROM inventory
                          JOIN items ON items.id = inventory.item_id WHERE inventory.user_id = ?""", (uid,), True)
    if not rows:
        bot.send_message(message.chat.id, "🎒 Инвентарь пуст. Загляни в 🛒 Магазин!")
        return
    text = "🎒 <b>Твой инвентарь:</b>\n\n" + "\n".join([f"{emoji} {name} × {qty}" for name, emoji, qty in rows])
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🎁 Ежедневный бонус")
def daily_bonus_cmd(message):
    uid = message.from_user.id
    ensure_player(uid, message.from_user.first_name)
    row = execute_db("SELECT last_daily FROM players WHERE user_id = ?", (uid,), True)
    last = row[0][0] if row else None
    now = time.time()
    if last:
        try:
            elapsed = now - float(last)
        except (TypeError, ValueError):
            elapsed = 999999
        if elapsed < 86400:
            remaining = int(86400 - elapsed)
            h, m = remaining // 3600, (remaining % 3600) // 60
            bot.send_message(message.chat.id, f"⏳ Бонус уже получен! Приходи через {h} ч {m} мин.")
            return
    reward = random.randint(20, 60)
    execute_db("UPDATE players SET рубли = рубли + ?, last_daily = ? WHERE user_id = ?", (reward, now, uid))
    bot.send_message(message.chat.id, f"🎁 Ежедневный бонус: +{reward} 💰 рублей!")

@bot.message_handler(func=lambda m: m.text == "🏆 Топ игроков")
def leaderboard_cmd(message):
    rows = execute_db("SELECT name, хуфа, рубли FROM players ORDER BY (хуфа * 10 + рубли) DESC LIMIT 10", (), True)
    if not rows:
        bot.send_message(message.chat.id, "🏆 Пока никто не набрал очков.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, huf, rub) in enumerate(rows):
        icon = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{icon} <b>{name}</b> — 🧪{huf} 💰{rub}")
    bot.send_message(message.chat.id, "🏆 <b>Топ игроков:</b>\n\n" + "\n".join(lines), parse_mode="HTML")

# ============================================================
# НОВЫЕ ФУНКЦИИ: ЗАКЛАДКИ ПО ВИКИ
# ============================================================

@bot.message_handler(commands=['bookmark'])
def bookmark_cmd(message):
    uid = message.from_user.id
    kw = clean_text(message.text.replace('/bookmark', ''), is_key=True).lower()
    if not kw:
        bot.reply_to(message, "❌ Формат: /bookmark ключевое_слово"); return
    if not get_wiki_info(kw):
        bot.reply_to(message, f"❌ «{kw}» не найдено в базе знаний."); return
    execute_db("INSERT OR IGNORE INTO bookmarks (user_id, keyword) VALUES (?, ?)", (uid, kw))
    bot.reply_to(message, f"🔖 «{kw}» добавлено в закладки!")

@bot.message_handler(func=lambda m: m.text == "🔖 Закладки")
def bookmarks_cmd(message):
    uid = message.from_user.id
    rows = execute_db("SELECT keyword FROM bookmarks WHERE user_id = ?", (uid,), True)
    if not rows:
        bot.send_message(message.chat.id, "🔖 Закладок нет. Добавь: /bookmark ключ")
        return
    text = "🔖 <b>Твои закладки:</b>\n\n" + "\n".join([f"• {kw}" for (kw,) in rows])
    text += "\n\nПросто напиши название, чтобы узнать подробности!"
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# ============================================================
# НОВЫЕ ФУНКЦИИ: АДМИН-ПАНЕЛЬ
# ============================================================

@bot.message_handler(func=lambda m: m.text == "🛠 Админ-панель" and m.from_user.id == ADMIN_ID)
def admin_panel_cmd(message):
    bot.send_message(message.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=admin_panel_kb(), parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💾 Бэкап БД" and m.from_user.id == ADMIN_ID)
def backup_cmd(message):
    try:
        with open(DB_NAME, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"💾 Бэкап от {time.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")
        bot.send_message(message.chat.id, "❌ Не удалось создать бэкап.")

@bot.message_handler(func=lambda m: m.text == "📈 Общая статистика" and m.from_user.id == ADMIN_ID)
def full_stats_cmd(message):
    users_n = execute_db("SELECT COUNT(*) FROM players", (), True)[0][0]
    wiki_n = execute_db("SELECT COUNT(*) FROM wiki", (), True)[0][0]
    stories_n = execute_db("SELECT COUNT(DISTINCT story_name) FROM stories", (), True)[0][0]
    links_n = execute_db("SELECT COUNT(*) FROM wiki_links", (), True)[0][0]
    sessions_n = execute_db("SELECT COUNT(*) FROM rp_sessions", (), True)[0][0]
    blocked_n = execute_db("SELECT COUNT(*) FROM blocked_users", (), True)[0][0]
    items_n = execute_db("SELECT COUNT(*) FROM items", (), True)[0][0]
    total_broadcast = len(get_all_users())
    text = (f"📈 <b>Общая статистика бота</b>\n\n"
            f"👤 Игроков зарегистрировано: {users_n}\n"
            f"📨 Всего в рассылке: {total_broadcast}\n"
            f"🚫 В бан-листе: {blocked_n}\n"
            f"📚 Записей в вики: {wiki_n}\n"
            f"🔗 Связей: {links_n}\n"
            f"📖 Историй: {stories_n}\n"
            f"🎭 РП-сессий (всего): {sessions_n}\n"
            f"🛒 Товаров в магазине: {items_n}")
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🚫 Бан-лист" and m.from_user.id == ADMIN_ID)
def banlist_menu_cmd(message):
    rows = execute_db("SELECT user_id, reason FROM blocked_users", (), True)
    text = "🚫 <b>Бан-лист:</b>\n\n" + ("\n".join([f"• <code>{uid}</code> — {reason or 'без причины'}" for uid, reason in rows]) if rows else "пусто")
    text += "\n\nКоманды:\n/ban ID [причина]\n/unban ID"
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Формат: /ban ID [причина]"); return
    try:
        target_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID должен быть числом."); return
    reason = parts[2] if len(parts) > 2 else None
    execute_db("INSERT OR REPLACE INTO blocked_users (user_id, reason) VALUES (?, ?)", (target_id, reason))
    bot.reply_to(message, f"🚫 Пользователь <code>{target_id}</code> заблокирован.", parse_mode="HTML")

@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ Формат: /unban ID"); return
    try:
        target_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID должен быть числом."); return
    execute_db("DELETE FROM blocked_users WHERE user_id = ?", (target_id,))
    bot.reply_to(message, f"✅ Пользователь <code>{target_id}</code> разблокирован.", parse_mode="HTML")

@bot.message_handler(commands=['give'])
def give_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.split()
    if len(parts) < 4 or parts[3] not in ('хуфа', 'рубли'):
        bot.reply_to(message, "❌ Формат: /give ID количество хуфа|рубли\nПример: /give 123456 50 рубли"); return
    try:
        target_id, amount = int(parts[1]), int(parts[2])
    except ValueError:
        bot.reply_to(message, "❌ ID и количество должны быть числами."); return
    currency = parts[3]
    ensure_player(target_id)
    execute_db(f"UPDATE players SET {currency} = {currency} + ? WHERE user_id = ?", (amount, target_id))
    bot.reply_to(message, f"✅ Выдано {amount} {currency} игроку <code>{target_id}</code>.", parse_mode="HTML")
    try:
        bot.send_message(target_id, f"🎉 Тебе начислено {amount} {currency} от Хранителя!")
    except Exception:
        pass

@bot.message_handler(func=lambda m: m.text == "💰 Выдать валюту" and m.from_user.id == ADMIN_ID)
def give_hint_cmd(message):
    bot.send_message(message.chat.id, "💰 Формат: /give ID количество хуфа|рубли\nПример: /give 123456 50 рубли")

@bot.message_handler(commands=['additem'])
def additem_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    text = message.text.replace('/additem', '').strip()
    parts = [p.strip() for p in text.split('|')]
    if len(parts) < 3:
        bot.reply_to(message, "❌ Формат: /additem Название | Цена | хуфа|рубли | Описание | Эмодзи\nПример: /additem Зелье силы | 30 | рубли | Даёт +2 к силе | 🧪")
        return
    name = parts[0]
    try:
        price = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Цена должна быть числом."); return
    currency = parts[2] if parts[2] in ('хуфа', 'рубли') else 'рубли'
    description = parts[3] if len(parts) > 3 else ""
    emoji = parts[4] if len(parts) > 4 else "🎁"
    execute_db("INSERT OR REPLACE INTO items (name, description, price, currency, emoji) VALUES (?, ?, ?, ?, ?)",
               (name, description, price, currency, emoji))
    bot.reply_to(message, f"✅ Товар «{emoji} {name}» добавлен в магазин за {price} {currency}!")

@bot.message_handler(func=lambda m: m.text == "🏷 Добавить товар" and m.from_user.id == ADMIN_ID)
def additem_hint_cmd(message):
    bot.send_message(message.chat.id, "🏷 Формат: /additem Название | Цена | хуфа|рубли | Описание | Эмодзи\nПример: /additem Зелье силы | 30 | рубли | Даёт +2 к силе | 🧪")

@bot.message_handler(func=lambda m: m.text == "📤 Экспорт вики" and m.from_user.id == ADMIN_ID)
def export_wiki_cmd(message):
    rows = execute_db("SELECT keyword, category, description FROM wiki ORDER BY category, keyword", (), True)
    if not rows:
        bot.send_message(message.chat.id, "📤 Вики пуста."); return
    lines = []
    for kw, cat, desc in rows:
        lines.append(f"### {kw} [{cat}]\n{desc}\n")
    export_path = os.path.join(BASE_DIR, 'wiki_export.txt')
    with open(export_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    with open(export_path, 'rb') as f:
        bot.send_document(message.chat.id, f, caption=f"📤 Экспорт вики ({len(rows)} записей)")

# ============================================================
# НОВЫЕ ФУНКЦИИ: ПОИСК И РЕКАП ДЛЯ РП
# ============================================================

@bot.message_handler(commands=['search'])
def search_cmd(message):
    query = message.text.replace('/search', '').strip()
    if not query:
        bot.reply_to(message, "❌ Формат: /search запрос"); return
    all_wiki = execute_db("SELECT keyword, category FROM wiki", (), True)
    q = query.lower()
    matches = [f"{CATEGORY_EMOJI.get(cat, '📚')} {kw}" for kw, cat in all_wiki if q in kw.lower()]
    if not matches:
        bot.reply_to(message, "🔍 Ничего не найдено."); return
    bot.reply_to(message, "🔍 <b>Найдено:</b>\n\n" + "\n".join(matches[:30]), parse_mode="HTML")

@bot.message_handler(commands=['recap'])
def recap_cmd(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
    if not active_chat:
        bot.reply_to(message, "❌ Нет активных сессий!"); return
    session = rp_sessions[active_chat]
    recent = session['context'][-30:]
    if not recent:
        bot.reply_to(message, "📖 Пока нечего пересказывать."); return
    context_text = "\n".join([f"{'ГМ' if c['user_id'] == 'gm' else c['user_name']}: {c['text'][:200]}" for c in recent])
    summary = groq_complete(
        "Ты — летописец ролевой игры. Кратко перескажи последние события сессии (5-8 предложений), выдели ключевые моменты и текущую интригу. Пиши на русском.",
        context_text, temperature=0.4, max_tokens=500
    )
    if summary:
        bot.send_message(uid, f"📖 <b>Краткий пересказ сессии:</b>\n\n{summary}", parse_mode="HTML")
    else:
        bot.reply_to(message, "⚠️ Не удалось составить пересказ.")

# ============================================================
# ГЛАВНЫЙ ОБРАБОТЧИК ВСЕХ СООБЩЕНИЙ
# ============================================================

@bot.message_handler(content_types=['text', 'photo', 'video', 'audio', 'voice', 'document', 'animation'])
def handle_all(message):
    uid = message.from_user.id
    chat_id = message.chat.id
    state = user_states.get(uid)

    # 0. Заблокированные пользователи полностью игнорируются
    if uid != ADMIN_ID and is_blocked(uid):
        return

    # 1. Обработка РП-сообщений (если активна сессия) — ПРОВЕРЯЕМ ПЕРВЫМИ
    if chat_id in rp_sessions and rp_sessions[chat_id].get('active'):
        if uid != rp_sessions[chat_id]['gm_id']:
            if message.content_type == 'text' and message.text and not message.text.startswith('/'):
                process_rp_message(chat_id, uid, message.text, message.from_user.first_name)
                return
            elif message.content_type != 'text':
                # Игроки могут отправлять медиа в РП
                process_rp_message(chat_id, uid, f"[Отправил {message.content_type}]", message.from_user.first_name)
                return

    # 2. Обработка текстовых сообщений
    if message.content_type == 'text' and message.text:
        text = message.text
        text_lower = text.lower()
        
        # Команды уже обработаны декораторами — просто выходим
        if text.startswith('/'):
            return
        
        # Обработка состояний
        if handle_state_message(message, uid, chat_id, state):
            return
        
        # Обработка кнопок меню
        if handle_menu_buttons(message, uid, chat_id, text, text_lower):
            return
        
        # Обработка специальных текстовых триггеров
        if handle_special_triggers(message, uid, chat_id, text, text_lower):
            return
        
        # Ответ админа на РП сообщение (reply)
        if uid == ADMIN_ID and message.reply_to_message:
            for chat_id_pending, pending in rp_pending.items():
                if message.reply_to_message.message_id in pending:
                    gm_reply_to_player(uid, text, message.reply_to_message.message_id)
                    bot.send_message(uid, "✅ Ответ отправлен!")
                    return
        
        # Поиск по вики (основной функционал)
        if can_search(state):
            answer, photo, key = search_wiki_with_context(message)
            if answer:
                safe_send(chat_id, answer, photo, key)
                return
    
    # 3. Обработка фото и других медиа
    if handle_media_message(message, uid, state):
        return
    
    # 4. Если ничего не сработало
    if message.content_type == 'text' and message.text == "🔙 Назад":
        bot.send_message(chat_id, "🔙 Главное меню", reply_markup=main_kb(uid))


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОБРАБОТЧИКА
# ============================================================

def can_search(state):
    """Проверяет, можно ли выполнять поиск по вики в текущем состоянии"""
    blocked_states = ['vs_mode', 'rp_name', 'rp_narrate_text', 'rp_context', 'edit_key', 'edit_desc']
    blocked_prefixes = ['story_', 'broadcast_', 'db_', 'reg_', 'learn_', 'link_']
    
    if state in blocked_states:
        return False
    if state and any(str(state).startswith(prefix) for prefix in blocked_prefixes):
        return False
    return True


def handle_state_message(message, uid, chat_id, state):
    """Обрабатывает сообщения в зависимости от текущего состояния пользователя"""
    if not state:
        return False
    
    text = message.text if message.content_type == 'text' and message.text else ""
    
    # Обработка состояний РП
    if uid == ADMIN_ID and state == 'rp_context':
        if message.content_type != 'text': return False
        context_topic = text
        status_msg = bot.send_message(uid, "🌐 Анализирую тему и ищу информацию...")
        context = analyze_chat_history_for_context(chat_id, context_topic)
        bot.delete_message(uid, status_msg.message_id)
        if context:
            temp_learning[uid]['context'] = context
            set_chat_context(chat_id, context)
            user_states[uid] = 'rp_name'
            bot.send_message(uid, f"📖 <b>Контекст создан:</b>\n{context[:600]}\n\n🎭 Введи название сессии (или /skip для авто-названия):")
        else:
            temp_learning[uid]['context'] = context_topic
            set_chat_context(chat_id, context_topic)
            user_states[uid] = 'rp_name'
            bot.send_message(uid, f"📖 Контекст: {context_topic[:300]}\n\n🎭 Введи название сессии (или /skip):")
        return True
    
    if uid == ADMIN_ID and state == 'rp_name':
        if message.content_type != 'text': return False
        if text == '/skip':
            text = temp_learning[uid].get('context', 'РП-сессия')[:50]
        context = temp_learning[uid].get('context', '')
        result, status = start_rp_session(temp_learning[uid]['rp_chat_id'], uid, text, context)
        if status == "no_context":
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Не удалось определить контекст. Нажмите «🎭 Начать сессию» и опишите тему.")
            return True
        rp_sessions[temp_learning[uid]['rp_chat_id']]['thread_id'] = temp_learning[uid].get('thread_id')
        user_states.pop(uid, None)
        bot.send_message(temp_learning[uid]['rp_chat_id'],
                        f"🎭 <b>РП-сессия началась!</b>\n«{text}»\n📖 Контекст: {context[:200]}",
                        parse_mode="HTML", message_thread_id=temp_learning[uid].get('thread_id'))
        bot.send_message(uid, f"✅ Сессия «{text}» запущена!\n📖 Контекст: {context[:300]}")
        return True
    
    if uid == ADMIN_ID and state == 'rp_narrate_text':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        active_chat = next((cid for cid, s in rp_sessions.items() if s['gm_id'] == uid and s['active']), None)
        if active_chat:
            gm_narrate(active_chat, text)
            bot.send_message(uid, "✅ Отправлено!", reply_markup=main_kb(uid))
        else:
            bot.send_message(uid, "❌ Нет активных сессий!")
        user_states.pop(uid, None)
        return True
    
    # Обработка состояний управления БД
    if uid == ADMIN_ID and state == 'db_view':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=db_management_kb())
            return True
        info = get_wiki_info(text)
        if info:
            msg = f"📋 <b>{info[0]}</b>\n\n📝 {info[1][:500]}\n\n{CATEGORY_EMOJI.get(info[3], '📚')} {info[3]}"
            links = get_links_text(info[0])
            if links: msg += f"\n\n🕸 Связи:\n{links}"
            safe_send(uid, msg, info[2])
        else:
            bot.send_message(uid, "❌ Не найдена!")
        user_states.pop(uid, None)
        return True
    
    if uid == ADMIN_ID and state == 'db_edit_key_old':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=db_management_kb())
            return True
        old_key = clean_text(text, is_key=True).lower()
        if not execute_db("SELECT keyword FROM wiki WHERE keyword = ?", (old_key,), True):
            bot.send_message(uid, "❌ Не найден!")
            return True
        temp_learning[uid] = {'old_key': old_key}
        user_states[uid] = 'db_edit_key_new'
        bot.send_message(uid, f"✏️ Новый ключ для <b>{old_key}</b>:")
        return True
    
    if uid == ADMIN_ID and state == 'db_edit_key_new':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=db_management_kb())
            return True
        success, msg = edit_wiki_keyword(temp_learning[uid]['old_key'], clean_text(text, is_key=True).lower())
        user_states.pop(uid, None)
        bot.send_message(uid, msg, reply_markup=db_management_kb())
        return True
    
    if uid == ADMIN_ID and state == 'db_update_photo_key':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=db_management_kb())
            return True
        keyword = clean_text(text, is_key=True).lower()
        if not execute_db("SELECT keyword FROM wiki WHERE keyword = ?", (keyword,), True):
            bot.send_message(uid, "❌ Не найден!")
            return True
        temp_learning[uid] = {'photo_key': keyword}
        user_states[uid] = 'db_update_photo_send'
        info = get_wiki_info(keyword)
        if info and info[2]:
            bot.send_photo(uid, info[2], caption="📸 Текущее фото\nОтправь НОВОЕ:")
        else:
            bot.send_message(uid, f"📸 Отправь фото для «{keyword}»:")
        return True
    
    if uid == ADMIN_ID and state == 'db_delete_photo':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=db_management_kb())
            return True
        success, msg = edit_wiki_photo(clean_text(text, is_key=True).lower(), None)
        user_states.pop(uid, None)
        bot.send_message(uid, msg, reply_markup=db_management_kb())
        return True
    
    # Обработка состояний связей
    if state == 'link_source':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        temp_learning[uid] = {'link_source': clean_text(text).lower()}
        user_states[uid] = 'link_target'
        bot.send_message(uid, f"🔗 Второй ключ для «{text}»:")
        return True
    
    if state == 'link_target':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        temp_learning[uid]['link_target'] = clean_text(text).lower()
        user_states[uid] = 'link_type'
        bot.send_message(uid, "🔗 Тип: враг | друг | союзник | находится_в | владеет | часть")
        return True
    
    if state == 'link_type':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        add_wiki_link(temp_learning[uid]['link_source'], temp_learning[uid]['link_target'], text.strip().lower())
        user_states.pop(uid, None)
        bot.send_message(uid, "✅ Связь добавлена!", reply_markup=main_kb(uid))
        return True
    
    # Обработка состояний рассылки
    if state and state.startswith('broadcast_'):
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        if state == 'broadcast_text':
            s, f = broadcast_message(uid, text)
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ {s}\n❌ {f}", reply_markup=main_kb(uid))
            return True
        if state == 'broadcast_photo':
            bot.send_message(uid, "❌ Отправь фото!")
            return True
    
    # Обработка VS битвы
    if state == 'vs_mode':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        for sep in [' vs ', ' против ']:
            if sep in text.lower():
                parts = text.lower().split(sep)
                if len(parts) == 2:
                    result = analyze_vs_battle(clean_text(parts[0]), clean_text(parts[1]))
                    if result:
                        safe_send(uid, result)
                    else:
                        bot.send_message(uid, "⚠️ Проверь имена.")
                    user_states.pop(uid, None)
                    bot.send_message(uid, "⚔️ Ещё?", reply_markup=main_kb(uid))
                    return True
        bot.send_message(uid, "❌ Формат: Имя1 vs Имя2")
        return True
    
    # Обработка состояний историй
    if state and state.startswith('story_'):
        if handle_story_state(message, uid, text, state):
            return True
    
    # Обработка состояний импорта
    if state == 'import_story':
        if message.content_type != 'text': return False
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        actual_name = find_story_by_name(text.strip())
        if not actual_name:
            bot.send_message(uid, "❌ История не найдена!")
            return True
        status_msg = bot.send_message(uid, "🤖 ИИ анализирует историю...", parse_mode="HTML")
        result = extract_lore_from_story(actual_name)
        bot.delete_message(uid, status_msg.message_id)
        user_states.pop(uid, None)
        if result:
            bot.send_message(uid, f"📥 <b>Найденные термины:</b>\n\n{result}", reply_markup=main_kb(uid))
        else:
            bot.send_message(uid, "⚠️ Не удалось извлечь лор.", reply_markup=main_kb(uid))
        return True
    
    # Обработка диалогового обучения
    if uid in dialogue_learning:
        if message.content_type != 'text': return False
        response = dialogue_learn_step(uid, text)
        if response:
            bot.send_message(uid, response, parse_mode="HTML")
        if uid not in dialogue_learning:
            bot.send_message(uid, "✅ Обучение завершено!", reply_markup=main_kb(uid))
        return True
    
    # Обработка обучения ГМ-а
    if uid == ADMIN_ID and state and state.startswith('learn_'):
        if handle_learn_state(message, uid, text, state):
            return True
    
    # Обработка регистрации
    if state and state.startswith('reg_'):
        if handle_reg_state(message, uid, state):
            return True
    
    # Обработка редактирования досье
    if uid == ADMIN_ID and state == 'edit_key':
        if message.content_type != 'text': return False
        clean_key = clean_text(text, is_key=True).lower()
        res = execute_db("SELECT description FROM wiki WHERE keyword = ?", (clean_key,), True)
        if res:
            temp_learning[uid] = {'key': clean_key}
            user_states[uid] = 'edit_desc'
            bot.send_message(uid, f"📝 Текущий:\n{res[0][0]}\n\nНовый текст:")
        else:
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Нет ключа.")
        return True
    
    if uid == ADMIN_ID and state == 'edit_desc':
        if message.content_type != 'text': return False
        execute_db("UPDATE wiki SET description = ? WHERE keyword = ?", (text, temp_learning[uid]['key']))
        user_states.pop(uid, None)
        bot.send_message(uid, "✅ Обновлено!", reply_markup=main_kb(uid))
        return True
    
    return False

print(">>> Модуль 5 загружен (коллбэки и часть обработчика)")

def handle_menu_buttons(message, uid, chat_id, text, text_lower):
    """Обрабатывает нажатия на кнопки клавиатуры"""
    
    # База знаний
    if "что ты знаешь" in text_lower or text_lower == "📚 база знаний":
        categories = get_categories_stats()
        if not categories:
            bot.send_message(chat_id, "🕸 Библиотека пуста...")
            return True
        bot.send_message(chat_id, f"📚 <b>База знаний Хуфы</b>\n📊 Записей: {sum(c for _, c in categories)}\n\n<b>Категории:</b>",
                        reply_markup=categories_kb(categories), parse_mode="HTML")
        return True
    
    # Админские кнопки
    if uid == ADMIN_ID:
        if text == "🤖 Авто-категоризация":
            msg = bot.send_message(uid, "🤖 Сортирую...")
            count = auto_categorize_all()
            bot.delete_message(uid, msg.message_id)
            bot.send_message(uid, f"✅ {count} записей!", reply_markup=main_kb(uid))
            return True
        
        if text == "🔧 Управление БД":
            bot.send_message(uid, "🔧 <b>Управление БД</b>", reply_markup=db_management_kb(), parse_mode="HTML")
            return True
        
        if text == "📋 Просмотр записи":
            keys = execute_db("SELECT keyword FROM wiki", (), True)
            if not keys:
                bot.send_message(uid, "📭 Пусто!")
                return True
            user_states[uid] = 'db_view'
            bot.send_message(uid, f"📋 Введи ключ:\n{', '.join([k[0] for k in keys])}")
            return True
        
        if text == "🔄 Изменить ключ":
            keys = execute_db("SELECT keyword FROM wiki", (), True)
            if not keys:
                bot.send_message(uid, "📭 Пусто!")
                return True
            user_states[uid] = 'db_edit_key_old'
            bot.send_message(uid, f"🔄 Старый ключ:\n{', '.join([k[0] for k in keys])}\n(/cancel)")
            return True
        
        if text == "🖼 Обновить фото":
            keys = execute_db("SELECT keyword FROM wiki", (), True)
            if not keys:
                bot.send_message(uid, "📭 Пусто!")
                return True
            user_states[uid] = 'db_update_photo_key'
            bot.send_message(uid, f"🖼 Ключ:\n{', '.join([k[0] for k in keys])}\n(/cancel)")
            return True
        
        if text == "🗑 Удалить фото":
            keys = execute_db("SELECT keyword, photo_id FROM wiki", (), True)
            keys_with_photos = [k[0] for k in keys if k[1]]
            if not keys_with_photos:
                bot.send_message(uid, "📭 Нет фото!")
                return True
            user_states[uid] = 'db_delete_photo'
            bot.send_message(uid, f"🗑 Ключ:\n{', '.join(keys_with_photos)}\n(/cancel)")
            return True
        
        if text == "🔙 Назад" and user_states.get(uid, '').startswith('db_'):
            user_states.pop(uid, None)
            bot.send_message(uid, "🔙 Главное меню", reply_markup=main_kb(uid))
            return True
        
        if text == "🔗 Связи":
            user_states[uid] = 'link_source'
            bot.send_message(uid, "🔗 Первый ключ:\n(/cancel)")
            return True
        
        if text == "📊 Статистика Лора":
            msg = get_lore_stats()
            conflicts = check_lore_conflicts()
            if conflicts:
                msg += "\n\n⚠️ " + "\n".join(conflicts)
            bot.send_message(uid, msg, parse_mode="HTML")
            return True
        
        if text == "🎲 Случайный Лор":
            lore = get_random_lore()
            if lore:
                safe_send(uid, f"🎲 <b>{lore[0].capitalize()}</b>\n\n{lore[1][:500]}", lore[2])
            else:
                bot.send_message(uid, "📭 Лор пуст!")
            return True
        
        if text == "❓ Викторина":
            quiz = generate_quiz()
            if quiz:
                quiz_data[uid] = quiz
                markup = types.InlineKeyboardMarkup()
                for i, opt in enumerate(quiz['options']):
                    markup.add(types.InlineKeyboardButton(opt.capitalize(), callback_data=f"quiz_{uid}_{i}_{quiz['correct']}"))
                bot.send_message(uid, quiz['question'], reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_message(uid, "📭 Недостаточно знаний (нужно 4+)!")
            return True
        
        if text == "💬 Диалог-обучение":
            response = dialogue_learn_step(uid, None)
            bot.send_message(uid, response, parse_mode="HTML")
            return True
        
        if text == "📢 Рассылка":
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("📢 Отправить текст", "🖼 Отправить с фото", "👥 Статистика", "🔙 Назад")
            bot.send_message(uid, "📢 <b>Рассылка</b>", reply_markup=markup, parse_mode="HTML")
            return True
        
        if text == "👥 Статистика":
            users = get_all_users()
            players = execute_db("SELECT COUNT(*) FROM players", (), True)
            wiki = execute_db("SELECT COUNT(*) FROM wiki", (), True)
            stories = get_all_stories()
            bot.send_message(uid, f"📊 Пользователей: {len(users)}\n🎭 Игроков: {players[0][0] if players else 0}\n📚 Знаний: {wiki[0][0] if wiki else 0}\n📖 Историй: {len(stories)}", parse_mode="HTML")
            return True
        
        if text == "📢 Отправить текст":
            user_states[uid] = 'broadcast_text'
            bot.send_message(uid, "📝 Текст:\n(/cancel)")
            return True
        
        if text == "🖼 Отправить с фото":
            user_states[uid] = 'broadcast_photo'
            bot.send_message(uid, "🖼 Фото с подписью:\n(/cancel)")
            return True
        
        if text == "⚔️ VS Битва":
            user_states[uid] = 'vs_mode'
            bot.send_message(uid, "⚔️ Имя1 vs Имя2\n(/cancel)")
            return True
        
        if text == "📖 Истории":
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("📝 Создать историю", "📚 Список историй", "🗑 Удалить историю", "⏹ Закончить историю", "🤖 Обработать историю", "🔙 Назад")
            bot.send_message(uid, "📖 <b>Истории</b>", reply_markup=markup, parse_mode="HTML")
            return True
        
        if text == "📝 Создать историю":
            user_states[uid] = 'story_name'
            bot.send_message(uid, "📝 Название:\n(/cancel)")
            return True
        
        if text == "📚 Список историй":
            stories = get_all_stories()
            if stories:
                bot.send_message(chat_id, "📚 <b>Истории:</b>\n" + "\n".join([f"📜 {s[0]} ({s[1]} ч.)" for s in stories]), parse_mode="HTML")
            else:
                bot.send_message(chat_id, "📭 Нет историй.")
            return True
        
        if text == "🗑 Удалить историю":
            stories = get_all_stories()
            if not stories:
                bot.send_message(uid, "📭 Нечего удалять!")
                return True
            user_states[uid] = 'story_delete'
            bot.send_message(uid, f"🗑 Название:\n{chr(10).join([f'• {s[0]}' for s in stories])}\n(/cancel)")
            return True
        
        if text == "🤖 Обработать историю":
            stories = get_all_stories()
            if not stories:
                bot.send_message(uid, "📭 Нет историй!")
                return True
            user_states[uid] = 'story_polish'
            bot.send_message(uid, f"🤖 Название:\n{chr(10).join([f'• {s[0]}' for s in stories])}\n(/cancel)")
            return True
        
        if text == "📥 Импорт из Истории":
            stories = get_all_stories()
            if not stories:
                bot.send_message(uid, "📭 Нет историй!")
                return True
            user_states[uid] = 'import_story'
            bot.send_message(uid, f"📥 Выбери историю:\n{chr(10).join([f'• {s[0]}' for s in stories])}\n\nВведи название (/cancel):", parse_mode="HTML")
            return True
        
        if text == "✏️ Редактировать досье":
            keys = execute_db("SELECT keyword FROM wiki", (), True)
            if not keys:
                bot.send_message(uid, "Пусто!")
                return True
            user_states[uid] = 'edit_key'
            bot.send_message(uid, f"🔑 Ключ:\n{', '.join([k[0] for k in keys])}")
            return True
        
        if text == "📜 Обучить ГМ-а":
            user_states[uid] = 'learn_key'
            bot.send_message(uid, "🔑 Ключ:")
            return True
    
    return False


def handle_special_triggers(message, uid, chat_id, text, text_lower):
    """Обрабатывает специальные текстовые триггеры"""
    
    # Расскажи историю
    if "расскажи историю" in text_lower:
        story_name = text_lower.replace("расскажи историю", "").strip()
        actual = find_story_by_name(story_name)
        if not actual:
            bot.send_message(chat_id, f"📭 «{story_name}» не найдена.")
            return True
        parts = get_story_parts(actual)
        if not parts:
            bot.send_message(chat_id, "📭 Пусто.")
            return True
        if chat_id not in story_tellers:
            story_tellers[chat_id] = {}
        story_tellers[chat_id][uid] = {'story_name': actual, 'current_part': 0, 'total_parts': len(parts)}
        send_story_part(chat_id, parts[0], 1, len(parts), actual)
        if len(parts) > 1:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("▶️ Продолжить историю", "⏹ Хватит")
            bot.send_message(chat_id, "▶️ «Продолжить историю»", reply_markup=markup)
        return True
    
    # Продолжить историю
    if text == "▶️ Продолжить историю":
        if chat_id not in story_tellers or uid not in story_tellers[chat_id]:
            return True
        info = story_tellers[chat_id][uid]
        next_part = info['current_part'] + 1
        parts = get_story_parts(info['story_name'])
        if next_part >= len(parts):
            bot.send_message(chat_id, "📖 Завершена!", reply_markup=main_kb(uid))
            del story_tellers[chat_id][uid]
            return True
        info['current_part'] = next_part
        send_story_part(chat_id, parts[next_part], next_part + 1, len(parts), info['story_name'])
        if next_part + 1 >= len(parts):
            del story_tellers[chat_id][uid]
        return True
    
    # Хватит истории
    if text == "⏹ Хватит":
        if chat_id in story_tellers and uid in story_tellers[chat_id]:
            del story_tellers[chat_id][uid]
        bot.send_message(chat_id, "📖 Прекратил.", reply_markup=main_kb(uid))
        return True
    
    # VS Битва (быстрый формат)
    if not text.startswith('/'):
        for sep in [' vs ', ' против ']:
            if sep in text.lower():
                parts = text.lower().split(sep)
                if len(parts) == 2:
                    result = analyze_vs_battle(clean_text(parts[0]), clean_text(parts[1]))
                    if result:
                        safe_send(chat_id, result)
                    else:
                        bot.send_message(chat_id, "⚠️ Проверь имена.")
                    return True
    
    # Ролевые сообщения (- * ")
    if text and text[0] in ['-', '*', '"'] and len(text) > 1:
        res = execute_db('SELECT name FROM players WHERE user_id = ?', (uid,), True)
        name = res[0][0] if res else "Странник"
        styles = {
            '-': f"<b>{name}</b>: — {text[1:]}",
            '*': f"<i>{name} {text[1:]}</i>",
            '"': f"💭 {name}: {text[1:]}"
        }
        try:
            bot.delete_message(chat_id, message.message_id)
        except:
            pass
        bot.send_message(chat_id, styles[text[0]], parse_mode="HTML")
        return True
    
    return False


def handle_media_message(message, uid, state):
    """Обрабатывает фото и другие медиа-сообщения"""
    
    # Отправка фото для рассылки
    if uid == ADMIN_ID and state == 'broadcast_photo':
        if message.photo:
            s, f = broadcast_message(uid, message.caption or "", message.photo[-1].file_id)
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ {s}\n❌ {f}", reply_markup=main_kb(uid))
            return True
    
    # Отправка фото для обновления фото в вики
    if uid == ADMIN_ID and state == 'db_update_photo_send':
        if message.photo:
            success, msg = edit_wiki_photo(temp_learning[uid]['photo_key'], message.photo[-1].file_id)
            user_states.pop(uid, None)
            bot.send_message(uid, msg, reply_markup=db_management_kb())
            if success:
                bot.send_photo(uid, message.photo[-1].file_id, caption=f"✅ Новое фото для «{temp_learning[uid]['photo_key']}»")
            return True
        else:
            bot.send_message(uid, "❌ Отправь фото!")
            return True
    
    # Фото для обучения (learn_photo)
    if uid == ADMIN_ID and state == 'learn_photo':
        if message.photo:
            p_id = message.photo[-1].file_id
            category = ai_categorize_keyword(temp_learning[uid]['key'], temp_learning[uid]['desc'])
            execute_db("INSERT OR REPLACE INTO wiki (keyword, description, photo_id, category) VALUES (?, ?, ?, ?)",
                      (temp_learning[uid]['key'], temp_learning[uid]['desc'], p_id, category))
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ Сохранено в «{category}»!", reply_markup=main_kb(uid))
            return True
    
    # Фото для регистрации
    if state == 'reg_photo':
        if message.photo:
            execute_db("INSERT INTO players (user_id, name, bio, photo) VALUES (?,?,?,?)",
                      (uid, temp_data[uid]['name'], temp_data[uid]['bio'], message.photo[-1].file_id))
            user_states.pop(uid, None)
            bot.send_message(uid, "✅ Герой создан!", reply_markup=main_kb(uid))
            return True
    
    # Фото/видео для истории
    if state == 'story_collect' and uid == ADMIN_ID:
        content, content_type, file_id = "", "text", None
        if message.content_type == 'photo':
            content = message.caption or "📸"
            content_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.content_type == 'video':
            content = message.caption or "🎥"
            content_type = "video"
            file_id = message.video.file_id
        else:
            return False
        
        story_parts[uid]['parts'].append({'content': content, 'type': content_type, 'file_id': file_id})
        bot.send_message(uid, f"✅ Часть {len(story_parts[uid]['parts'])} добавлена!")
        return True
    
    return False


def handle_story_state(message, uid, text, state):
    """Обрабатывает состояния работы с историями"""
    
    if state == 'story_name':
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        if get_story_count(clean_text(text)) > 0:
            bot.send_message(uid, "⚠️ Существует!")
            return True
        story_parts[uid] = {'name': clean_text(text), 'parts': []}
        user_states[uid] = 'story_collect'
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📝 Создать историю", "📚 Список историй", "🗑 Удалить историю",
                   "⏹ Закончить историю", "🤖 Обработать историю", "🔙 Назад")
        bot.send_message(uid, f"📝 Собираю «{clean_text(text)}»\nПересылай сообщения. «⏹» когда готово.", reply_markup=markup)
        return True
    
    if state == 'story_delete':
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        delete_story(clean_text(text))
        user_states.pop(uid, None)
        bot.send_message(uid, "✅ Удалена!", reply_markup=main_kb(uid))
        return True
    
    if state == 'story_polish':
        if text == '/cancel':
            user_states.pop(uid, None)
            bot.send_message(uid, "❌ Отменено.", reply_markup=main_kb(uid))
            return True
        actual = find_story_by_name(clean_text(text))
        if not actual:
            bot.send_message(uid, "❌ Не найдена!")
            return True
        polished = polish_full_story(actual)
        if polished:
            save_story_part(actual, get_story_count(actual) + 1, polished)
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ Готово! Часть {get_story_count(actual)}.", reply_markup=main_kb(uid))
        else:
            bot.send_message(uid, "⚠️ Не удалось.", reply_markup=main_kb(uid))
        return True
    
    if state == 'story_collect' and uid == ADMIN_ID:
        if text == "⏹ Закончить историю":
            if story_parts[uid]['parts']:
                for i, part in enumerate(story_parts[uid]['parts'], 1):
                    save_story_part(story_parts[uid]['name'], i, part['content'], part['type'], part.get('file_id'))
                count = len(story_parts[uid]['parts'])
                del story_parts[uid]
                user_states.pop(uid, None)
                bot.send_message(uid, f"✅ Сохранена! ({count} ч.)", reply_markup=main_kb(uid))
            else:
                user_states.pop(uid, None)
                bot.send_message(uid, "❌ Нечего сохранять!", reply_markup=main_kb(uid))
            return True
        
        if text == "🔙 Назад":
            if uid in story_parts:
                del story_parts[uid]
            user_states.pop(uid, None)
            bot.send_message(uid, "🔙 Главное меню.", reply_markup=main_kb(uid))
            return True
        
        if message.content_type == 'text':
            story_parts[uid]['parts'].append({'content': text, 'type': 'text', 'file_id': None})
            bot.send_message(uid, f"✅ Часть {len(story_parts[uid]['parts'])} добавлена!")
            return True
    
    return False


def handle_learn_state(message, uid, text, state):
    """Обрабатывает состояния обучения ГМ-а"""
    
    if state == 'learn_key':
        temp_learning[uid] = {'key': clean_text(text, is_key=True).lower()}
        user_states[uid] = 'learn_desc_or_generate'
        bot.send_message(uid, f"📝 Описание для «{text}» или /generate\n(/skip для фото)")
        return True
    
    if state == 'learn_desc_or_generate':
        if text == '/generate':
            generated = generate_wiki_description(temp_learning[uid]['key'])
            if generated:
                temp_learning[uid]['desc'] = generated
                user_states[uid] = 'learn_photo'
                bot.send_message(uid, f"🤖 {generated}\n\n📸 Фото или /skip:")
            else:
                bot.send_message(uid, "⚠️ Не удалось.")
            return True
        if text == '/skip':
            temp_learning[uid]['desc'] = ""
            user_states[uid] = 'learn_photo'
            bot.send_message(uid, "📸 Фото или /skip:")
            return True
        temp_learning[uid]['desc'] = text
        user_states[uid] = 'learn_photo'
        bot.send_message(uid, "📸 Фото или /skip:")
        return True
    
    if state == 'learn_photo':
        if text == '/skip':
            category = ai_categorize_keyword(temp_learning[uid]['key'], temp_learning[uid]['desc'])
            execute_db("INSERT OR REPLACE INTO wiki (keyword, description, photo_id, category) VALUES (?, ?, ?, ?)",
                      (temp_learning[uid]['key'], temp_learning[uid]['desc'], None, category))
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ Сохранено в «{category}»!", reply_markup=main_kb(uid))
            return True
        return False  # Ждём фото
    
    return False


def handle_reg_state(message, uid, state):
    """Обрабатывает состояния регистрации игрока"""
    
    if message.content_type != 'text':
        return False
    
    text = message.text
    
    if state == 'reg_name':
        temp_data[uid] = {'name': clean_text(text)}
        user_states[uid] = 'reg_bio'
        bot.send_message(uid, "📖 Био:")
        return True
    
    if state == 'reg_bio':
        temp_data[uid]['bio'] = text
        user_states[uid] = 'reg_photo'
        bot.send_message(uid, "📸 Фото:")
        return True
    
    return False

print(">>> Модуль 6 загружен (обработчики меню и триггеров)")
# ============================================================
# ЗАПУСК БОТА
# ============================================================

if __name__ == '__main__':
    logger.info("БОТ ЗАПУСКАЕТСЯ...")
    logger.info("Инициализация базы данных...")
    init_db()
    logger.info("Миграция базы данных...")
    migrate_db()
    logger.info("Все модули загружены успешно!")
    logger.info("🕯 Библиотека Хуфы готова к работе")
    logger.info("БОТ ЗАПУЩЕН!")

    import warnings
    warnings.filterwarnings("ignore")

    # Бесконечный опрос Telegram с авто-перезапуском при сбоях сети/API,
    # чтобы падение одного запроса не останавливало бота насовсем.
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Опрос Telegram упал: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)