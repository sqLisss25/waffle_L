import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
    CallbackQuery,
)
from openai import OpenAI

from secret import TELEGRAM_TOKEN, OPENROUTER_TOKEN

# ---------------------- Логирование ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------- Конфигурация ----------------------
BOT_TOKEN = TELEGRAM_TOKEN
OPENAI_CLIENT = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_TOKEN,
)

USERS_FILE = "users.json"
RECIPES_FILE = "recipes.json"
DEFAULT_PLAN_DAYS = 3

# ---------------------- Загрузка / сохранение ----------------------
def load_json(filename: str):
    if not os.path.exists(filename):
        logger.info(f"Файл {filename} не найден, возвращаю пустой объект")
        return {} if "users" in filename else []
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "recipes" in filename and not isinstance(data, list):
        logger.warning(f"recipes.json имеет неверный тип, заменён на []")
        data = []
    return data

def save_json(filename: str, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.debug(f"Файл {filename} сохранён")

users_db = load_json(USERS_FILE)
recipes_db = load_json(RECIPES_FILE)

def get_user(user_id: int) -> Optional[Dict]:
    return users_db.get(str(user_id))

def save_user(user_id: int, data: Dict):
    users_db[str(user_id)] = data
    save_json(USERS_FILE, users_db)
    logger.info(f"Пользователь {user_id} сохранён")

# ---------------------- FSM ----------------------
class Quiz(StatesGroup):
    gender = State()
    age = State()
    cooking_frequency = State()
    priorities = State()
    meals = State()
    height = State()
    weight = State()
    persons = State()

class Setting(StatesGroup):
    wait_gender = State()
    wait_age = State()
    wait_freq = State()
    wait_priorities = State()
    wait_meals = State()
    wait_height = State()
    wait_weight = State()
    wait_persons = State()

# Константы
MALE, FEMALE = "male", "female"
AGE_OPTIONS = ["< 18", "18-20", "21-23", "24-26", "27-30", "> 30"]
FREQ_OPTIONS = [
    ("every_day", "Каждый день"),
    ("2_3_days", "Раз в 2-3 дня"),
    ("1_2_week", "1-2 раза в неделю"),
]
PRIORITY_OPTIONS = [
    ("money", "Экономия денег 💰"),
    ("time", "Экономия времени ⏱️"),
    ("diversity", "Разнообразие блюд 🌈"),
    ("health", "Здоровое питание 🥗"),
    ("taste", "Вкус и удовольствие 🍽️"),
]
MEAL_OPTIONS = [
    ("breakfast", "Завтрак"),
    ("second_breakfast", "Второй завтрак"),
    ("lunch", "Обед"),
    ("snack", "Перекус"),
    ("dinner", "Ужин"),
]

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✨ Создать план")],
        [KeyboardButton(text="📋 Мой план")],
        [KeyboardButton(text="⚙️ Настройки")],
    ],
    resize_keyboard=True,
)

# ---------------------- Утилиты ----------------------
def make_toggle_keyboard(options, selected: List[str], prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for value, text in options:
        check = "✅ " if value in selected else ""
        buttons.append([InlineKeyboardButton(text=f"{check}{text}", callback_data=f"{prefix}_toggle_{value}")])
    buttons.append([InlineKeyboardButton(text="Готово ✅", callback_data=f"{prefix}_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def toggle_selection(selected: List[str], value: str) -> List[str]:
    if value in selected:
        selected.remove(value)
    else:
        selected.append(value)
    return selected

def calculate_bmr(gender: str, age_str: str, height: int, weight: int) -> float:
    if "-" in age_str:
        low, high = age_str.split("-")
        age = (int(low) + int(high)) // 2
    elif age_str.startswith("<"):
        age = 17
    elif age_str.startswith(">"):
        age = 32
    else:
        age = int(age_str)
    if gender == "male":
        return 10 * weight + 6.25 * height - 5 * age + 5
    else:
        return 10 * weight + 6.25 * height - 5 * age - 161

def get_activity_factor(freq: str) -> tuple:
    if freq == "every_day":
        return 1.55, "высокий"
    elif freq == "2_3_days":
        return 1.375, "средний"
    else:
        return 1.2, "низкий"

def get_target_calories(user: Dict) -> int:
    bmr = calculate_bmr(user["gender"], user["age"], user["height"], user["weight"])
    factor, _ = get_activity_factor(user["cooking_frequency"])
    return int(bmr * factor * 0.9)  # минус 10%

# ---------------------- Роутеры ----------------------
quiz_router = Router()
main_router = Router()
plan_router = Router()
settings_router = Router()

# ================== АНКЕТА ==================
async def start_full_quiz(message: Message, state: FSMContext):
    logger.info(f"Запущена анкета для пользователя {message.from_user.id}")
    await message.answer("Давай познакомимся! Сейчас я задам несколько вопросов.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мужской", callback_data="gender_male"),
         InlineKeyboardButton(text="Женский", callback_data="gender_female")]
    ])
    await message.answer("Выбери свой пол:", reply_markup=keyboard)
    await state.set_state(Quiz.gender)

@quiz_router.callback_query(StateFilter(Quiz.gender), F.data.startswith("gender_"))
async def process_gender(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[1]
    await state.update_data(gender=gender)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Вы выбрали: {'мужской' if gender == 'male' else 'женский'} пол.")
    logger.info(f"Пользователь {callback.from_user.id} выбрал пол: {gender}")
    kb = [[InlineKeyboardButton(text=age, callback_data=f"age_{age}")] for age in AGE_OPTIONS]
    await callback.message.answer("Укажи свой возраст:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(Quiz.age)
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.age), F.data.startswith("age_"))
async def process_age(callback: CallbackQuery, state: FSMContext):
    age = callback.data.split("_", 1)[1]
    await state.update_data(age=age)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Возраст: {age}")
    logger.info(f"Пользователь {callback.from_user.id} выбрал возраст: {age}")
    kb = [[InlineKeyboardButton(text=text, callback_data=f"freq_{value}")] for value, text in FREQ_OPTIONS]
    await callback.message.answer("Как часто ты готовишь?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(Quiz.cooking_frequency)
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.cooking_frequency), F.data.startswith("freq_"))
async def process_freq(callback: CallbackQuery, state: FSMContext):
    freq = callback.data.split("_", 1)[1]
    await state.update_data(cooking_frequency=freq)
    await callback.message.edit_reply_markup(reply_markup=None)
    freq_text = dict(FREQ_OPTIONS)[freq]
    await callback.message.answer(f"✅ Частота готовки: {freq_text}")
    logger.info(f"Пользователь {callback.from_user.id} выбрал частоту: {freq}")
    await state.update_data(priorities=[])
    keyboard = make_toggle_keyboard(PRIORITY_OPTIONS, [], "priority")
    await callback.message.answer("Что для тебя важнее всего при планировании питания? Выбери один или несколько:", reply_markup=keyboard)
    await state.set_state(Quiz.priorities)
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.priorities), F.data.startswith("priority_toggle_"))
async def toggle_priority(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("priorities", [])
    value = callback.data.replace("priority_toggle_", "")
    selected = toggle_selection(selected, value)
    await state.update_data(priorities=selected)
    await callback.message.edit_reply_markup(reply_markup=make_toggle_keyboard(PRIORITY_OPTIONS, selected, "priority"))
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.priorities), F.data == "priority_done")
async def priorities_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("priorities", [])
    if not selected:
        await callback.answer("Выберите хотя бы один вариант!", show_alert=True)
        return
    names = [text for val, text in PRIORITY_OPTIONS if val in selected]
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Вы выбрали: {', '.join(names)}")
    logger.info(f"Пользователь {callback.from_user.id} выбрал приоритеты: {selected}")
    await state.update_data(meals=[])
    keyboard = make_toggle_keyboard(MEAL_OPTIONS, [], "meal")
    await callback.message.answer("Какие приёмы пищи тебе нужны? Выбери один или несколько:", reply_markup=keyboard)
    await state.set_state(Quiz.meals)
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.meals), F.data.startswith("meal_toggle_"))
async def toggle_meal(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("meals", [])
    value = callback.data.replace("meal_toggle_", "")
    selected = toggle_selection(selected, value)
    await state.update_data(meals=selected)
    await callback.message.edit_reply_markup(reply_markup=make_toggle_keyboard(MEAL_OPTIONS, selected, "meal"))
    await callback.answer()

@quiz_router.callback_query(StateFilter(Quiz.meals), F.data == "meal_done")
async def meals_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("meals", [])
    if not selected:
        await callback.answer("Выберите хотя бы один приём пищи!", show_alert=True)
        return
    names = [text for val, text in MEAL_OPTIONS if val in selected]
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Вы выбрали: {', '.join(names)}")
    logger.info(f"Пользователь {callback.from_user.id} выбрал приёмы: {selected}")
    await callback.message.answer("Введи свой рост в сантиметрах (например, 170):")
    await state.set_state(Quiz.height)
    await callback.answer()

@quiz_router.message(StateFilter(Quiz.height))
async def process_height(message: Message, state: FSMContext):
    try:
        h = int(message.text.strip())
        if not 100 <= h <= 250: raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введи число от 100 до 250 см.")
        return
    await state.update_data(height=h)
    await message.answer(f"✅ Рост: {h} см")
    logger.info(f"Пользователь {message.from_user.id} ввёл рост: {h}")
    await message.answer("Теперь введи свой вес в килограммах (например, 65):")
    await state.set_state(Quiz.weight)

@quiz_router.message(StateFilter(Quiz.weight))
async def process_weight(message: Message, state: FSMContext):
    try:
        w = int(message.text.strip())
        if not 30 <= w <= 250: raise ValueError
    except ValueError:
        await message.answer("Введи целое число от 30 до 250 кг.")
        return
    await state.update_data(weight=w)
    await message.answer(f"✅ Вес: {w} кг")
    logger.info(f"Пользователь {message.from_user.id} ввёл вес: {w}")
    await message.answer("На сколько человек рассчитывать рецепты? (например, 2):")
    await state.set_state(Quiz.persons)

@quiz_router.message(StateFilter(Quiz.persons))
async def process_persons(message: Message, state: FSMContext):
    try:
        p = int(message.text.strip())
        if not 1 <= p <= 20: raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 20.")
        return
    await state.update_data(persons=p)
    data = await state.get_data()
    uid = message.from_user.id
    profile = {
        "gender": data["gender"],
        "age": data["age"],
        "cooking_frequency": data["cooking_frequency"],
        "priorities": data["priorities"],
        "meals": data["meals"],
        "height": data["height"],
        "weight": data["weight"],
        "persons": data["persons"],
    }
    old = get_user(uid)
    if old and "current_plan" in old:
        profile["current_plan"] = old["current_plan"]
    save_user(uid, profile)
    await state.clear()
    await message.answer("🎉 Анкета сохранена! Теперь ты можешь создать план питания.", reply_markup=main_menu)
    logger.info(f"Анкета пользователя {uid} успешно завершена и сохранена")

# ================== ГЛАВНОЕ МЕНЮ ==================
@main_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    logger.info(f"Пользователь {message.from_user.id} вызвал /start")
    user = get_user(message.from_user.id)
    if user is None:
        await start_full_quiz(message, state)
    else:
        await message.answer("👋 С возвращением! Выбери действие:", reply_markup=main_menu)

@main_router.message(F.text == "📋 Мой план")
async def show_my_plan(message: Message):
    user = get_user(message.from_user.id)
    if not user or "current_plan" not in user:
        await message.answer("У тебя пока нет плана питания. Создай его с помощью кнопки ✨ Создать план.")
        return
    plan = user["current_plan"]
    text = format_short_plan(plan)
    keyboard = build_plan_navigation_keyboard(plan, "overview")
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@main_router.message(F.text == "✨ Создать план")
async def create_plan(message: Message, state: FSMContext):
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        await message.answer("Сначала заполни анкету с помощью /start")
        return
    logger.info(f"Пользователь {uid} запросил создание плана")
    await message.answer("⏳ Составляю план питания... Это может занять до минуты.")
    try:
        plan, error = await generate_plan(uid)
    except Exception as e:
        logger.exception(f"Ошибка генерации плана для {uid}: {e}")
        await message.answer(f"❌ Произошла ошибка при генерации плана: {e}")
        return
    if error:
        logger.warning(f"Не удалось сгенерировать план для {uid}: {error}")
        await message.answer(f"❌ {error}")
        return
    logger.info(f"План для {uid} успешно сгенерирован")
    text = format_short_plan(plan)
    keyboard = build_plan_navigation_keyboard(plan, "overview")
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

# ================== НАСТРОЙКИ ==================
def format_user_settings(user: Dict) -> str:
    freq = dict(FREQ_OPTIONS).get(user["cooking_frequency"], user["cooking_frequency"])
    prio = ", ".join([text for val, text in PRIORITY_OPTIONS if val in user["priorities"]])
    meals = ", ".join([text for val, text in MEAL_OPTIONS if val in user["meals"]])
    return (
        f"⚙️ <b>Текущие настройки</b>\n"
        f"• Пол: {'мужской' if user['gender']=='male' else 'женский'}\n"
        f"• Возраст: {user['age']}\n"
        f"• Частота готовки: {freq}\n"
        f"• Приоритеты: {prio}\n"
        f"• Приёмы пищи: {meals}\n"
        f"• Рост: {user['height']} см\n"
        f"• Вес: {user['weight']} кг\n"
        f"• Количество персон: {user['persons']}\n\n"
        f"Что хочешь изменить?"
    )

def settings_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пол", callback_data="set_gender")],
        [InlineKeyboardButton(text="📅 Возраст", callback_data="set_age")],
        [InlineKeyboardButton(text="⏱ Частота готовки", callback_data="set_freq")],
        [InlineKeyboardButton(text="🎯 Приоритеты", callback_data="set_priorities")],
        [InlineKeyboardButton(text="🍽 Приёмы пищи", callback_data="set_meals")],
        [InlineKeyboardButton(text="📏 Рост", callback_data="set_height")],
        [InlineKeyboardButton(text="⚖️ Вес", callback_data="set_weight")],
        [InlineKeyboardButton(text="👥 Количество персон", callback_data="set_persons")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])

@main_router.message(F.text == "⚙️ Настройки")
async def show_settings(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету с помощью /start")
        return
    await state.clear()
    logger.info(f"Пользователь {message.from_user.id} открыл настройки")
    await message.answer(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")

@settings_router.callback_query(F.data == "settings_cancel")
async def cancel_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("👋 Настройки не изменены.", reply_markup=None)
    await callback.answer()

@settings_router.callback_query(F.data.startswith("set_"))
async def start_setting(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split("_", 1)[1]
    user = get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден")
        return
    await state.clear()
    logger.info(f"Пользователь {callback.from_user.id} начал изменение поля: {field}")
    if field == "gender":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Мужской", callback_data="setval_gender_male"),
             InlineKeyboardButton(text="Женский", callback_data="setval_gender_female")]
        ])
        await callback.message.edit_text("Выбери пол:", reply_markup=keyboard)
        await state.set_state(Setting.wait_gender)
    elif field == "age":
        kb = [[InlineKeyboardButton(text=age, callback_data=f"setval_age_{age}")] for age in AGE_OPTIONS]
        await callback.message.edit_text("Укажи возраст:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await state.set_state(Setting.wait_age)
    elif field == "freq":
        kb = [[InlineKeyboardButton(text=text, callback_data=f"setval_freq_{value}")] for value, text in FREQ_OPTIONS]
        await callback.message.edit_text("Как часто готовишь?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await state.set_state(Setting.wait_freq)
    elif field == "priorities":
        await state.update_data(priorities=user.get("priorities", []))
        keyboard = make_toggle_keyboard(PRIORITY_OPTIONS, user.get("priorities", []), "setval_prio")
        await callback.message.edit_text("Выбери приоритеты:", reply_markup=keyboard)
        await state.set_state(Setting.wait_priorities)
    elif field == "meals":
        await state.update_data(meals=user.get("meals", []))
        keyboard = make_toggle_keyboard(MEAL_OPTIONS, user.get("meals", []), "setval_meal")
        await callback.message.edit_text("Выбери приёмы пищи:", reply_markup=keyboard)
        await state.set_state(Setting.wait_meals)
    elif field == "height":
        await callback.message.edit_text("Введи новый рост (см):")
        await state.set_state(Setting.wait_height)
    elif field == "weight":
        await callback.message.edit_text("Введи новый вес (кг):")
        await state.set_state(Setting.wait_weight)
    elif field == "persons":
        await callback.message.edit_text("Введи количество персон:")
        await state.set_state(Setting.wait_persons)
    await callback.answer()

# ----- Обработчики изменений (одиночный выбор) -----
@settings_router.callback_query(StateFilter(Setting.wait_gender), F.data.startswith("setval_gender_"))
async def set_gender(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[-1]
    uid = callback.from_user.id
    user = get_user(uid)
    user["gender"] = gender
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил пол на {gender}")
    await callback.message.edit_text(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")
    await callback.answer(f"Пол изменён на {'мужской' if gender=='male' else 'женский'}")

@settings_router.callback_query(StateFilter(Setting.wait_age), F.data.startswith("setval_age_"))
async def set_age(callback: CallbackQuery, state: FSMContext):
    age = callback.data.split("_", 2)[2]
    uid = callback.from_user.id
    user = get_user(uid)
    user["age"] = age
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил возраст на {age}")
    await callback.message.edit_text(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")
    await callback.answer(f"Возраст изменён на {age}")

@settings_router.callback_query(StateFilter(Setting.wait_freq), F.data.startswith("setval_freq_"))
async def set_freq(callback: CallbackQuery, state: FSMContext):
    freq = callback.data.split("_", 2)[2]
    uid = callback.from_user.id
    user = get_user(uid)
    user["cooking_frequency"] = freq
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил частоту готовки на {freq}")
    await callback.message.edit_text(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")
    await callback.answer("Частота изменена")

# ----- Множественный выбор (приоритеты, приёмы) -----
@settings_router.callback_query(StateFilter(Setting.wait_priorities), F.data.startswith("setval_prio_toggle_"))
async def toggle_prio_setting(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("priorities", [])
    value = callback.data.replace("setval_prio_toggle_", "")
    selected = toggle_selection(selected, value)
    await state.update_data(priorities=selected)
    await callback.message.edit_reply_markup(reply_markup=make_toggle_keyboard(PRIORITY_OPTIONS, selected, "setval_prio"))
    await callback.answer()

@settings_router.callback_query(StateFilter(Setting.wait_priorities), F.data == "setval_prio_done")
async def prio_setting_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("priorities", [])
    if not selected:
        await callback.answer("Выберите хотя бы один вариант!", show_alert=True)
        return
    uid = callback.from_user.id
    user = get_user(uid)
    user["priorities"] = selected
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил приоритеты на {selected}")
    await callback.message.edit_text(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")
    await callback.answer("Приоритеты обновлены")

@settings_router.callback_query(StateFilter(Setting.wait_meals), F.data.startswith("setval_meal_toggle_"))
async def toggle_meal_setting(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("meals", [])
    value = callback.data.replace("setval_meal_toggle_", "")
    selected = toggle_selection(selected, value)
    await state.update_data(meals=selected)
    await callback.message.edit_reply_markup(reply_markup=make_toggle_keyboard(MEAL_OPTIONS, selected, "setval_meal"))
    await callback.answer()

@settings_router.callback_query(StateFilter(Setting.wait_meals), F.data == "setval_meal_done")
async def meal_setting_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("meals", [])
    if not selected:
        await callback.answer("Выберите хотя бы один приём пищи!", show_alert=True)
        return
    uid = callback.from_user.id
    user = get_user(uid)
    user["meals"] = selected
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил приёмы пищи на {selected}")
    await callback.message.edit_text(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")
    await callback.answer("Приёмы пищи обновлены")

# ----- Текстовые поля -----
@settings_router.message(StateFilter(Setting.wait_height))
async def set_height(message: Message, state: FSMContext):
    try:
        h = int(message.text.strip())
        if not 100 <= h <= 250: raise ValueError
    except ValueError:
        await message.answer("Введи число от 100 до 250 см.")
        return
    uid = message.from_user.id
    user = get_user(uid)
    user["height"] = h
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил рост на {h}")
    await message.answer(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")

@settings_router.message(StateFilter(Setting.wait_weight))
async def set_weight(message: Message, state: FSMContext):
    try:
        w = int(message.text.strip())
        if not 30 <= w <= 250: raise ValueError
    except ValueError:
        await message.answer("Введи число от 30 до 250 кг.")
        return
    uid = message.from_user.id
    user = get_user(uid)
    user["weight"] = w
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил вес на {w}")
    await message.answer(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")

@settings_router.message(StateFilter(Setting.wait_persons))
async def set_persons(message: Message, state: FSMContext):
    try:
        p = int(message.text.strip())
        if not 1 <= p <= 20: raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 20.")
        return
    uid = message.from_user.id
    user = get_user(uid)
    user["persons"] = p
    save_user(uid, user)
    await state.clear()
    logger.info(f"Пользователь {uid} изменил количество персон на {p}")
    await message.answer(format_user_settings(user), reply_markup=settings_inline_keyboard(), parse_mode="HTML")

# ------------------- LLM -------------------
def build_full_plan_prompt(user: Dict, recipes: List[Dict], days: int) -> str:
    target_calories = get_target_calories(user)
    persons = user["persons"]
    meal_types = user["meals"]

    short_recipes = []
    for r in recipes:
        if not isinstance(r, dict):
            continue
        short_recipes.append({
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "calories_per_serving": r["calories"],
            "servings": r["servings"],
            "cooking_time": r["cooking_time"],
            "ingredients": r["ingredients"],
            "instructions": r["instructions"],
            "tags": r.get("tags", []),
            "description": r.get("description", ""),
        })

    prompt = f"""
Ты — профессиональный диетолог и шеф-повар. Составь план питания на {days} дней.

Параметры:
- Пол: {user['gender']}
- Возраст: {user['age']}
- Рост: {user['height']} см
- Вес: {user['weight']} кг
- Количество персон: {persons}
- Нужные приёмы пищи: {', '.join(meal_types)}
- Целевая калорийность на день: {target_calories} ккал

База рецептов (JSON):
{json.dumps(short_recipes, ensure_ascii=False, indent=2)}

ПРАВИЛА (строгие):
1. Для каждого дня выбери ровно по ОДНОМУ рецепту на каждый тип приёма пищи из списка {meal_types}. Не больше, не меньше.
2. Каждый день должен иметь уникальный набор блюд – нельзя повторять те же рецепты во всех днях. Допустимо повторение блюда не чаще 1 раза на все дни.
3. Для каждого блюда пересчитай количество ингредиентов: умножь каждый amount на {persons} / servings. Итоговая калорийность блюда = calories_per_serving * {persons} / servings (округли до целых).
4. Суммарная калорийность дня (total_calories) должна быть {target_calories} ккал (±5%).
5. Для каждого блюда напиши подробные инструкции, адаптированные под новый объём.
6. Ответ строго в JSON внутри ```json ... ```, без единого слова вне блока.

Формат ответа (точно такой):
```json
{{
  "days": [
    {{
      "day": 1,
      "meals": [
        {{
          "type": "breakfast",
          "recipe_id": "id",
          "name": "Название",
          "calories": 500,
          "ingredients": [{{"name": "...", "amount": 2, "unit": "шт"}}],
          "instructions": "Шаг 1...\\nШаг 2..."
        }},
        ... // по одному объекту на каждый тип из {meal_types}
      ],
      "total_calories": {target_calories}
    }}
  ]
}}
```
ВАЖНО:
- Все строки экранированы для JSON: двойные кавычки внутри строк замени на \\\", переносы строк — на \\n.
- Поле meals содержит ровно {len(meal_types)} объектов, каждый с уникальным type.
- Не выдумывай отсутствующие типы приёмов пищи.
"""
    return prompt

async def call_llm(prompt: str, system: str = "Ты полезный ассистент, отвечающий на русском языке.") -> str:
    logger.info("Отправка запроса к LLM")
    try:
        response = OPENAI_CLIENT.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b:free",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            timeout=120,
        )
        logger.info("Ответ от LLM получен")
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка запроса к LLM: {e}")
        raise

def extract_json(text: str) -> Optional[Any]:
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        text = text[start:end+1]

    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1)
    else:
        candidate = text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        logger.warning(f"Не удалось распарсить JSON: {e}. Начало кандидата: {candidate[:200]}")
        return None

def validate_plan(plan: Dict, user_meals: List[str]) -> bool:
    if "days" not in plan:
        return False
    for day in plan["days"]:
        meals = day.get("meals", [])
        if len(meals) != len(user_meals):
            logger.warning(f"День {day.get('day')}: ожидалось {len(user_meals)} приёмов, получено {len(meals)}")
            return False
        types = [m.get("type") for m in meals]
        if sorted(types) != sorted(user_meals):
            logger.warning(f"День {day.get('day')}: типы приёмов {types} не совпадают с {user_meals}")
            return False
        if len(set(types)) != len(types):
            logger.warning(f"День {day.get('day')}: дублирование типов приёма пищи")
            return False
    return True

async def generate_plan(user_id: int):
    user = get_user(user_id)
    if not user:
        return None, "Профиль не найден."

    logger.info(f"Генерация плана для пользователя {user_id}")
    prompt = build_full_plan_prompt(user, recipes_db, DEFAULT_PLAN_DAYS)

    # Первая попытка
    raw = await call_llm(prompt)
    plan_data = extract_json(raw)

    # Проверяем структуру
    if plan_data and "days" in plan_data:
        if not validate_plan(plan_data, user["meals"]):
            logger.warning("План не прошёл валидацию. Отправляем модели на исправление.")
            fix_prompt = (
                "Твой предыдущий ответ содержит ошибки в структуре плана. "
                "Убедись, что в каждом дне meals содержит ровно по одному объекту для каждого из типов: "
                f"{', '.join(user['meals'])}.\n"
                "Все дни должны быть разными (не копируй одинаковые наборы).\n"
                "Верни исправленный JSON внутри ```json ... ```.\n"
                "Вот твой предыдущий ответ (для контекста):\n"
                f"{raw}"
            )
            raw2 = await call_llm(fix_prompt)
            plan_data = extract_json(raw2)
            if plan_data and "days" in plan_data:
                if not validate_plan(plan_data, user["meals"]):
                    logger.error("Исправленный план всё ещё не валиден.")
                    return None, "Не удалось составить корректный план. Попробуйте позже."
            else:
                logger.error("Не удалось распарсить исправленный JSON.")
                return None, "Ошибка обработки данных. Попробуйте ещё раз."

    if not plan_data or "days" not in plan_data:
        logger.warning("Первый ответ не валидный. Пробую исправить JSON.")
        fix_prompt = (
            "Твой предыдущий ответ не является валидным JSON. Вот он:\n"
            f"{raw}\n\n"
            "Пожалуйста, исправь его так, чтобы он стал корректным JSON, который можно распарсить с помощью json.loads(). "
            "Убедись, что все строки экранированы (кавычки, переносы строк). "
            "Верни ТОЛЬКО исправленный JSON внутри ```json ... ```, без лишнего текста."
        )
        raw2 = await call_llm(fix_prompt)
        plan_data = extract_json(raw2)
        if plan_data and "days" in plan_data:
            if not validate_plan(plan_data, user["meals"]):
                logger.error("После исправления JSON план не валиден.")
                return None, "План не соответствует требованиям."
        else:
            logger.error("Не удалось получить валидный JSON даже после исправления.")
            return None, "Не удалось сгенерировать план. Попробуйте позже."

    # Сохраняем
    user["current_plan"] = {
        "created_at": datetime.now().isoformat(),
        "days": plan_data["days"],
    }
    save_user(user_id, user)
    logger.info(f"План для {user_id} сохранён")
    return user["current_plan"], None

def format_short_plan(plan: Dict) -> str:
    lines = ["<b>🎯 Ваш план питания</b>\n"]
    for day in plan["days"]:
        lines.append(f"<b>День {day['day']}</b>")
        for meal in day["meals"]:
            meal_type = {
                "breakfast": "Завтрак", "lunch": "Обед",
                "dinner": "Ужин", "snack": "Перекус",
                "second_breakfast": "Второй завтрак"
            }.get(meal["type"], meal["type"])
            lines.append(f"{meal_type}: {meal['name']} (~{meal['calories']} ккал)")
        lines.append(f"<i>Сумма: {day.get('total_calories', '—')} ккал</i>\n")
    lines.append("<i>Выбери день, чтобы увидеть детали и рецепты</i>")
    return "\n".join(lines)

def build_plan_navigation_keyboard(plan: Dict, step: str = "overview", current_day: int = 1) -> InlineKeyboardMarkup:
    if step == "overview":
        buttons = [[InlineKeyboardButton(text=f"День {d['day']}", callback_data=f"planday_{d['day']}")] for d in plan["days"]]
        buttons.append([InlineKeyboardButton(text="📋 Список покупок", callback_data="shopping_list")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    elif step == "detail":
        total_days = len(plan["days"])
        prev_day = current_day - 1 if current_day > 1 else total_days
        next_day = current_day + 1 if current_day < total_days else 1
        day_data = next((d for d in plan["days"] if d["day"] == current_day), None)
        buttons = []
        if day_data:
            for meal in day_data["meals"]:
                label = {"breakfast":"Завтрак","lunch":"Обед","dinner":"Ужин","snack":"Перекус","second_breakfast":"Второй завтрак"}.get(meal["type"], meal["type"])
                buttons.append([InlineKeyboardButton(text=f"🔄 Заменить {label}", callback_data=f"replace_{current_day}_{meal['type']}")])
        nav_row = [
            InlineKeyboardButton(text=f"<< День {prev_day}", callback_data=f"planday_{prev_day}"),
            InlineKeyboardButton(text=f"День {next_day} >>", callback_data=f"planday_{next_day}")
        ]
        buttons.append(nav_row)
        buttons.append([InlineKeyboardButton(text="↩️ Назад к обзору", callback_data="plan_overview")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    return InlineKeyboardMarkup(inline_keyboard=[])

def format_day_detail(day_data: Dict) -> str:
    lines = [f"<b>План питания на День {day_data['day']}</b>\n"]
    for meal in day_data["meals"]:
        t = {"breakfast":"Завтрак","lunch":"Обед","dinner":"Ужин","snack":"Перекус","second_breakfast":"Второй завтрак"}.get(meal["type"], meal["type"])
        lines.append(f"<b>{t}:</b> {meal['name']} (~{meal['calories']} ккал)")
        if "ingredients" in meal:
            ingr = ", ".join([f"{i['name']} ({i['amount']}{i['unit']})" for i in meal["ingredients"]])
            lines.append(f"<b>Ингредиенты:</b> {ingr}")
        if "instructions" in meal:
            instr = meal["instructions"].replace("\\n", "\n")
            lines.append(f"<b>Подробный рецепт:</b>")
            lines.append(f"<blockquote>{instr}</blockquote>")
        lines.append("")
    lines.append(f"<b>Общая калорийность:</b> {day_data.get('total_calories', '—')} ккал")
    return "\n".join(lines)

# ================== PLAN CALLBACKS ==================
@plan_router.callback_query(F.data == "plan_overview")
async def overview_callback(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user or "current_plan" not in user:
        await callback.answer("План не найден")
        return
    plan = user["current_plan"]
    await callback.message.edit_text(format_short_plan(plan), reply_markup=build_plan_navigation_keyboard(plan, "overview"), parse_mode="HTML")
    await callback.answer()

@plan_router.callback_query(F.data.startswith("planday_"))
async def show_day_detail(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user or "current_plan" not in user:
        await callback.answer("План не найден")
        return
    day = int(callback.data.split("_")[1])
    logger.info(f"Пользователь {callback.from_user.id} открыл день {day}")
    plan = user["current_plan"]
    day_data = next((d for d in plan["days"] if d["day"] == day), None)
    if not day_data:
        await callback.answer("День не найден")
        return
    await callback.message.edit_text(
        format_day_detail(day_data),
        reply_markup=build_plan_navigation_keyboard(plan, "detail", day),
        parse_mode="HTML",
    )

@plan_router.callback_query(F.data.startswith("replace_"))
async def replace_meal(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 3: return
    day = int(parts[1])
    meal_type = parts[2]
    uid = callback.from_user.id
    user = get_user(uid)
    if not user or "current_plan" not in user:
        await callback.answer("План не найден")
        return
    await callback.answer("⏳ Подбираю замену...")
    await callback.message.edit_text("⏳ Ищу подходящий рецепт...")

    prompt = (
        f"Пользователь хочет заменить приём пищи: день {day}, тип {meal_type}.\n"
        f"Текущий план: {json.dumps(user['current_plan'], ensure_ascii=False)}\n"
        f"Полная база рецептов: {json.dumps(recipes_db, ensure_ascii=False)}\n"
        f"Предпочтения: {json.dumps(user, ensure_ascii=False)}\n"
        f"Выбери новый рецепт, адаптируй под {user['persons']} чел.\n"
        "Верни ТОЛЬКО JSON внутри ```json...``` со структурой, идентичной текущему плану (с полем days). "
        "Замени только нужный день и приём, остальные оставь без изменений."
    )
    try:
        raw = await call_llm(prompt)
        new_plan_data = extract_json(raw)
        if not new_plan_data or "days" not in new_plan_data:
            await callback.message.edit_text("❌ Не удалось заменить блюдо.")
            return
        user["current_plan"] = {"created_at": datetime.now().isoformat(), "days": new_plan_data["days"]}
        save_user(uid, user)
        day_data = next((d for d in new_plan_data["days"] if d["day"] == day), None)
        if day_data:
            await callback.message.edit_text(
                format_day_detail(day_data),
                reply_markup=build_plan_navigation_keyboard(new_plan_data, "detail", day),
                parse_mode="HTML",
            )
        else:
            await callback.message.edit_text("❌ Ошибка в новом плане.")
    except Exception as e:
        logger.exception(f"Ошибка замены: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {e}")

@plan_router.callback_query(F.data == "shopping_list")
async def shopping_list_callback(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user or "current_plan" not in user:
        await callback.answer("План не найден")
        return
    plan = user["current_plan"]
    prompt = f"""
Составь список покупок для плана питания (JSON ниже) на {user['persons']} чел.
Сгруппируй по категориям, укажи итоговые количества.
План:
{json.dumps(plan, ensure_ascii=False, indent=2)}
"""
    try:
        raw = await call_llm(prompt)
        await callback.message.answer(f"<b>📋 Список покупок:</b>\n{raw}", parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
    await callback.answer()

# ================== ЗАПУСК ==================
async def main():
    logger.info("Запуск бота")
    bot = Bot(token=BOT_TOKEN, timeout=60)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.include_router(quiz_router)
    dp.include_router(settings_router)
    dp.include_router(main_router)
    dp.include_router(plan_router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот начал поллинг")
    await dp.start_polling(bot, timeout=60)

if __name__ == "__main__":
    asyncio.run(main())
