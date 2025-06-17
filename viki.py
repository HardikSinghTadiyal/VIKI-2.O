import os
import webbrowser
import datetime
import speech_recognition as sr
import pyttsx3
import subprocess
import time
import threading
import wikipedia
import requests
from bs4 import BeautifulSoup
import urllib.request
import re
import json
import html # Import the html module for HTML entity unescaping
# import langdetect # Uncomment this if you implement automatic language detection

# Initialize the speech engine
try:
    engine = pyttsx3.init()
    # Attempt to set a default speaking rate and volume
    engine.setProperty('rate', 180) # words per minute
    engine.setProperty('volume', 0.9) # 0.0 to 1.0
    print("[DEBUG] pyttsx3 engine initialized successfully.")
except Exception as e:
    engine = None
    print(f"Warning: pyttsx3 initialization failed. Text-to-speech functionality will be disabled. Error: {e}")

# Initialize recognizer
recognizer = sr.Recognizer()

# --- Configuration for Gemini API ---
# The API_KEY is left blank; the Canvas environment will inject it for fetch calls.
# If running locally, you would need to insert your actual Gemini API key here.
API_KEY = "AIzaSyCTsPr1M3sqqtlwon63TT0KhsY3UugjECg"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={API_KEY}"

# This list will store the chat history for API calls to maintain conversation context
chat_history = []

# --- Global Events for Speech Interruption ---
is_speaking_event = threading.Event() # Set when bot is actively speaking
_stop_speaking_event = threading.Event() # Set by another thread to request speech stop
_speaking_thread = None # Reference to the thread currently speaking
chat_mode = False  # Flag to indicate if chatbot mode is active

# --- Multi-language Support Configuration ---
# Maps user-friendly language names to Google Speech Recognition language codes
LANGUAGE_MAP = {
    "english": "en-US",
    "spanish": "es-ES",
    "hindi": "hi-IN",
    "french": "fr-FR",
    "german": "de-DE",
    "japanese": "ja-JP",
    "korean": "ko-KR",
    # Add more languages as needed and as supported by SR and TTS
}
# Default language
current_language = LANGUAGE_MAP["english"]

def get_lang_display_name(lang_code):
    """Returns the user-friendly name for a given language code."""
    for name, code in LANGUAGE_MAP.items():
        if code == lang_code:
            return name.capitalize()
    return lang_code # Return code if not found


# --- Text Sanitization Function ---
def clean_markdown_for_tts(text):
    """
    Removes common Markdown formatting and other problematic symbols from text
    to make it more suitable for Text-to-Speech (TTS) engines.
    """
    # 1. Decode common HTML entities (e.g., &amp; -> &)
    text = html.unescape(text)

    # 2. Remove markdown bold/italic markers (**text**, __text__, *text*, _text_)
    # This specifically targets actual markdown syntax.
    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text) # For **bold** and __bold__
    text = re.sub(r'(\*|_)(.*?)\1', r'\2', text)   # For *italic* and _italic_

    # 3. Remove inline/block code blocks (```code``` and `code`)
    text = re.sub(r'`{1,3}(.*?)`{1,3}', r'\1', text, flags=re.DOTALL)

    # 4. Remove headers (# Heading) - only at the start of a line
    text = re.sub(r'^\s*#+\s*', '', text, flags=re.MULTILINE)

    # 5. Remove link markdown: [link text](url) becomes "link text"
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)

    # 6. Remove list markers (- or * for unordered, and numbers like 1. for ordered lists)
    # This specifically targets markers at the beginning of a line.
    text = re.sub(r'^\s*[\-\*\d\.]+\s+', '', text, flags=re.MULTILINE)

    # 7. Remove blockquotes (>) - only at the start of a line
    text = re.sub(r'^\s*>\s*', '', text, flags=re.MULTILINE)

    # 8. Aggressively remove any character that is NOT:
    #    - an English alphabet letter (a-z, A-Z)
    #    - a digit (0-9)
    #    - standard whitespace (\s: space, tab, newline, etc.)
    #    - common punctuation that aids natural speech (.,!?).
    #    This is the primary safeguard against "scrambling" from unexpected symbols.
    text = re.sub(r'[^a-zA-Z0-9\s.,!?]', '', text)

    # 9. Replace multiple newlines with a single space to avoid unnatural pauses
    text = re.sub(r'\n+', ' ', text)

    # 10. Replace multiple spaces with a single space and strip leading/trailing whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text

# --- Speech and Chatbot Functions ---

def speak_in_thread_internal(text_to_speak, stop_event):
    """
    Internal function to run TTS in a separate thread, allowing interruption.
    Breaks text into sentences for more granular control.
    """
    global engine
    if engine is None:
        print("TTS disabled: " + text_to_speak)
        is_speaking_event.clear()
        return

    print(f"[DEBUG] speak_in_thread_internal() speaking: {text_to_speak}")  # Debug print

    # Select voice based on current_language
    # This attempts to find a voice that matches the language code
    # Actual voice availability depends on the OS and installed language packs
    try:
        voices = engine.getProperty('voices')
        lang_prefix = current_language.split('-')[0].lower() # e.g., 'en', 'es', 'hi'
        found_voice = None
        for voice in voices:
            # pyttsx3 voices on Windows do not have 'lang' attribute, use 'id' or 'name' instead
            if lang_prefix in voice.id.lower() or lang_prefix in voice.name.lower():
                found_voice = voice.id
                break
        
        if found_voice:
            engine.setProperty('voice', found_voice)
        else:
            print(f"Warning: No voice found for language '{current_language}'. Using default.")
    except Exception as e:
        print(f"Error selecting voice: {e}")



    # Split text into sentences for more granular interruption
    sentences = re.split(r'(?<=[.!?])\s+', text_to_speak)

    for sentence in sentences:
        if stop_event.is_set():
            engine.stop() # Stop current utterance
            break # Exit the loop, stopping the speech
        engine.say(sentence.strip())
        try:
            engine.runAndWait() # This will block until the sentence is spoken
        except RuntimeError as e:
            print(f"RuntimeError in runAndWait: {e}")
            # Attempt to stop and restart the engine
            engine.stop()

    engine.stop() # Ensure engine is stopped after all sentences or interruption
    is_speaking_event.clear() # Clear the flag once speaking is truly done or interrupted
    _stop_speaking_event.clear() # Reset stop event for next time


import threading

_speak_lock = threading.Lock()

def speak(text):
    """
    Starts speaking the given text in a separate daemon thread.
    Sanitizes text and allows for external interruption via _stop_speaking_event.
    """
    global _speaking_thread, _stop_speaking_event, is_speaking_event, engine

    with _speak_lock:
        print(f"[DEBUG] speak() called with text: {text}")  # Debug print

        # If already speaking, signal to stop the current speech before starting a new one
        if is_speaking_event.is_set():
            print("[DEBUG] speak() detected ongoing speech, stopping it first.")
            _stop_speaking_event.set() # Signal current speaking thread to stop
            if _speaking_thread and _speaking_thread.is_alive():
                # Give it a small timeout to gracefully finish its current sentence
                _speaking_thread.join(timeout=0.1)
            # Clear the flag and event regardless, ensuring a clean state for new speech
            is_speaking_event.clear()
            _stop_speaking_event.clear()
            _speaking_thread = None
            print("[DEBUG] speak() cleared speaking events and thread reference.")

        sanitized_text = clean_markdown_for_tts(text)

        is_speaking_event.set() # Indicate that new speech is starting
        _speaking_thread = threading.Thread(target=speak_in_thread_internal, args=(sanitized_text, _stop_speaking_event), daemon=True)
        _speaking_thread.start()
        print("[DEBUG] speak() started new speaking thread.")

def stop_current_speech():
    """Immediately stops any ongoing speech from the TTS engine."""
    global _stop_speaking_event, is_speaking_event, engine, _speaking_thread

    with _speak_lock:
        if is_speaking_event.is_set():
            print("[DEBUG] stop_current_speech() called, stopping speech.")
            _stop_speaking_event.set() # Signal the speaking thread to stop
            if engine:
                try:
                    engine.stop() # Force stop the pyttsx3 engine immediately
                except Exception as e:
                    print(f"Error stopping engine: {e}")
            # Give the thread a moment to recognize the stop signal and terminate
            if _speaking_thread and _speaking_thread.is_alive():
                _speaking_thread.join(timeout=0.1)
            is_speaking_event.clear() # Clear the flag
            _stop_speaking_event.clear() # Reset the stop signal
            _speaking_thread = None
            print("[DEBUG] stop_current_speech() cleared speaking events and thread reference.")
            print("Speech interrupted by external request.") # Debugging print statement

def get_gemini_response(prompt):
    """
    Sends a prompt to the Gemini API with the entire chat history for context
    and returns the bot's response. The response is sanitized for TTS.
    """
    # Add user message to chat history for context
    chat_history.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "contents": chat_history # Send the entire conversation history
    }

    try:
        response = requests.post(API_URL, json=payload, headers={'Content-Type': 'application/json'})
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)

        result = response.json()

        bot_response_text = "Sorry, I couldn't get a response. Please try again."
        if result.get("candidates") and len(result["candidates"]) > 0 and \
           result["candidates"][0].get("content") and \
           result["candidates"][0]["content"].get("parts") and \
           len(result["candidates"][0]["content"]["parts"]) > 0:
            raw_text = result["candidates"][0]["content"].get("parts")[0]["text"]
            
            # Sanitize the raw text before speaking and adding to history
            bot_response_text = clean_markdown_for_tts(raw_text)

            # Add bot response to chat history (the cleaned version)
            chat_history.append({"role": "model", "parts": [{"text": bot_response_text}]})
        else:
            print(f"Error: Unexpected API response structure: {json.dumps(result, indent=2)}")

        return bot_response_text

    except requests.exceptions.RequestException as e:
        return f"Error connecting to Gemini API: {e}"
    except json.JSONDecodeError:
        return "Error: Could not decode JSON response from API."
    except Exception as e:
        return f"An unexpected error occurred: {e}"

def recognize_speech(timeout=None):
    """
    Listens for speech input from the microphone and converts it to text.
    Uses the current_language for recognition.
    """
    with sr.Microphone() as source:
        print(f"Listening for speech in {get_lang_display_name(current_language)}...")
        recognizer.adjust_for_ambient_noise(source)
        try:
            audio = recognizer.listen(source, timeout=timeout) # Use timeout here
            query = recognizer.recognize_google(audio, language=current_language) # Use current_language
            print(f"User said: {query}")
            return query
        except sr.UnknownValueError:
            print(f"Sorry, I didn't catch that in {get_lang_display_name(current_language)} (or no speech detected within timeout).")
            return None
        except sr.WaitTimeoutError: # Catch timeout specifically
            print("No speech detected within the timeout period.")
            return None
        except sr.RequestError as e:
            print(f"Could not request results from Google Speech Recognition service; {e}")
            return None

# --- Utility Functions ---

def open_chrome():
    """Opens Google Chrome browser."""
    try:
        # This path might need to be adjusted based on the user's system
        subprocess.Popen("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        return "Opening Google Chrome."
    except FileNotFoundError:
        return "Chrome browser not found on your system. Please ensure it's installed or update the path."

def set_reminder(reminder_text, delay_seconds):
    """
    Sets a reminder to speak a message after a specified delay.
    Runs in a separate thread.
    """
    def reminder():
        time.sleep(delay_seconds)
        # This speak here is for the reminder itself, not the initial confirmation
        speak(f"Reminder: {reminder_text}")
    reminder_thread = threading.Thread(target=reminder)
    reminder_thread.daemon = True # Allows the main program to exit even if this thread is running
    reminder_thread.start()
    if delay_seconds < 60:
        time_str = f"{delay_seconds} seconds"
    elif delay_seconds < 3600:
        time_str = f"{delay_seconds // 60} minutes"
    else:
        time_str = f"{delay_seconds // 3600} hours"
    return f"Reminder set for {time_str} from now."

SEARCH_ENGINE_ID = "82b9d3ed58f984546" # This might be a placeholder or specific CSE ID

def search_google_and_read(query):
    """
    Opens Google search results for the given query in a web browser.
    """
    try:
        # Use the Google search URL directly for general searches
        search_url = f"https://www.google.com/search?q={query}"
        webbrowser.open(search_url)
        return "Search results are on your screen."
    except Exception as e:
        return f"Sorry, I had trouble opening the search page. Error: {e}"

# --- Custom Commands Management ---

CUSTOM_COMMANDS_FILE = "custom_commands.json"

# Global list to track active reminder threads and their stop events
active_reminders = []

def load_custom_commands():
    """
    Loads custom application/webapp mappings from a JSON file.
    """
    if os.path.exists(CUSTOM_COMMANDS_FILE):
        with open(CUSTOM_COMMANDS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: {CUSTOM_COMMANDS_FILE} is empty or malformed. Starting with no custom commands.")
                return {}
    return {}

def save_custom_commands(commands):
    """
    Saves custom application/webapp mappings to a JSON file.
    """
    with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(commands, f, indent=4)

custom_commands = load_custom_commands()

def cancel_all_reminders():
    """
    Cancels all active reminders by setting their stop events.
    """
    global active_reminders
    for reminder_thread, stop_event in active_reminders:
        stop_event.set()
    active_reminders.clear()

def set_reminder(reminder_text, delay_seconds):
    """
    Sets a reminder to speak a message after a specified delay.
    Runs in a separate thread.
    """
    stop_event = threading.Event()

    def reminder():
        # Wait for delay or stop event
        if not stop_event.wait(timeout=delay_seconds):
            # This speak here is for the reminder itself, not the initial confirmation
            speak(f"Reminder: {reminder_text}")
        # Remove this reminder from active list when done or stopped
        active_reminders.remove((threading.current_thread(), stop_event))

    reminder_thread = threading.Thread(target=reminder)
    reminder_thread.daemon = True # Allows the main program to exit even if this thread is running
    active_reminders.append((reminder_thread, stop_event))
    reminder_thread.start()
    if delay_seconds < 60:
        time_str = f"{delay_seconds} seconds"
    elif delay_seconds < 3600:
        time_str = f"{delay_seconds // 60} minutes"
    else:
        time_str = f"{delay_seconds // 3600} hours"
    return f"Reminder set for {time_str} from now."

# --- Main Task Performance Function ---

def perform_task(query):
    """
    Analyzes the user's query and performs the corresponding action.
    This is the core logic for command recognition and chatbot fallback.
    It returns the response string to be spoken by the UI.
    """
    global chat_mode # Access the global chat_mode flag
    global current_language # Access global current language

    if query is None:
        return None

    # Reload custom commands to ensure the latest are used (especially if edited via UI)
    global custom_commands
    custom_commands = load_custom_commands()

    query_lower = query.lower().strip()
    print(f"Recognized query: '{query_lower}'")
    print("Available voice commands:")
    for vc in custom_commands.keys():
        print(f" - '{vc}'")

    # --- Multi-language Commands ---
    for lang_name, lang_code in LANGUAGE_MAP.items():
        if f"switch to {lang_name}" in query_lower:
            stop_current_speech()
            old_lang_name = get_lang_display_name(current_language)
            current_language = lang_code
            return f"Language switched from {old_lang_name} to {lang_name}. I will now listen and respond in {lang_name}."
        elif f"habla en {lang_name}" in query_lower and lang_name == "spanish": # Example specific phrase
            stop_current_speech()
            old_lang_name = get_lang_display_name(current_language)
            current_language = lang_code
            return f"Cambiando de {old_lang_name} a español. Ahora escucharé y responderé en español."


    # Commands to enter or exit chat mode
    if "start chat" in query_lower:
        stop_current_speech() # Stop any current speech
        chat_mode = True
        return "Chat mode activated. You can now talk to me like a chatbot."
    elif "end chat" in query_lower or "exit chat" in query_lower:
        stop_current_speech() # Stop any current speech
        chat_mode = False
        return "Chat mode deactivated. Returning to command mode."
    elif "reset chat" in query_lower:
        stop_current_speech() # Stop any current speech
        chat_history.clear()
        return "Chat history has been reset."
    elif any(phrase in query_lower for phrase in ["ok i got it", "ok done", "okay done", "alright done", "stop", "shush", "quiet", "cancel", "hold on", "enough"]):
        stop_current_speech() # Explicitly stop current speech
        return "interrupted" # Special return value for UI to handle

    if chat_mode:
        # In chat mode, send all queries to Gemini chatbot
        print("Chat mode active. Sending query to Gemini chatbot.")
        response = get_gemini_response(query)
        return response

    if "show chat history" in query_lower:
        stop_current_speech()
        if not chat_history:
            return "There is no chat history to show."
        
        history_texts = ["Here is the chat history:"]
        for entry in chat_history:
            role = entry.get("role", "unknown")
            parts = entry.get("parts", [])
            for part in parts:
                text = part.get("text", "")
                history_texts.append(f"{role.capitalize()}: {text}")
        full_history_text = "\n".join(history_texts)
        return full_history_text


    # 1. Check custom commands first
    for voice_cmd, app_path in custom_commands.items():
        voice_cmd_lower = voice_cmd.lower().strip()
        # Match if exact or if voice command is a separate word in query
        if query_lower == voice_cmd_lower or f" {voice_cmd_lower} " in f" {query_lower} ":
            print(f"Matched voice command: '{voice_cmd}' with path: '{app_path}'")
            try:
                if app_path.startswith("web://"):
                    webapp_url = app_path[len("web://"):]
                    webbrowser.open(webapp_url)
                    return f"Opening web application {webapp_url}"
                else:
                    if os.path.isfile(app_path):
                        subprocess.Popen(app_path)
                        return f"Opening {os.path.basename(app_path)}"
                    else:
                        return f"The path {app_path} does not exist."
            except Exception as e:
                return f"Failed to open {os.path.basename(app_path)}. Error: {str(e)}"

    # 2. Handle predefined system commands
    if "hello" in query_lower:
        return "Hey there! What can I do for you today?"

    elif "what's your name" in query_lower:
        return "I'm Viky, your friendly assistant. How can I help?"

    elif "what is the time" in query_lower:
        current_time = datetime.datetime.now().strftime("%I:%M %p")
        return f"It's {current_time} right now."

    elif "open google" in query_lower:
        webbrowser.open("https://www.google.com")
        return "Opening Google."

    elif "open notepad" in query_lower:
        subprocess.Popen("notepad.exe")
        return "Opening Notepad."

    elif "open calculator" in query_lower:
        subprocess.Popen("calc.exe")
        return "Opening Calculator."

    elif "open word" in query_lower:
        try:
            subprocess.Popen(["winword.exe"])
            return "Opening Microsoft Word."
        except FileNotFoundError:
            return "Microsoft Word is not installed on this computer."

    elif "open excel" in query_lower:
        try:
            subprocess.Popen(["excel.exe"])
            return "Opening Microsoft Excel."
        except FileNotFoundError:
            return "Microsoft Excel is not installed on this computer."

    elif "open chrome" in query_lower:
        return open_chrome() # open_chrome now returns a string

    elif "open youtube" in query_lower:
        webbrowser.open("https://www.youtube.com/")
        return "Opening YouTube."

    elif "time for workout" in query_lower or "start workout" in query_lower:
        webbrowser.open("https://workout.lol/")
        return "Time for a workout!"
        
    # New command for opening OpenAI website
    elif "open AI" in query_lower or "open open AI" in query_lower:
        webbrowser.open("https://openai.com/")
        return "Opening OpenAI website."

    elif "play music" in query_lower:
        # Note: play music will ask for a follow-up query, the UI's _perform_task_and_display
        # will need to handle this as it expects a single returnable response.
        # For full conversational flows within perform_task, you'd need more complex state management
        # or a function calling pattern with the LLM.
        return "What song would you like me to play?"

    elif "search" in query_lower and len(query_lower.split()) > 1: # Ensure "search" is not the only word
        search_term = query_lower.replace("search", "").strip()
        if search_term:
            return f"Searching Google for {search_term}."
        else:
            return "What would you like me to search for?"

    elif "wikipedia" in query_lower:
        # Similar to play music, this requires a follow-up query.
        return "What would you like to know about on Wikipedia?"

    elif "exit" in query_lower or "stop" in query_lower or "quit" in query_lower:
        stop_current_speech() # Ensure it stops speaking immediately
        return "Goodbye!" # Signal to main loop

    # 3. Fallback to general chatbot (Gemini) if no specific command is recognized
    else:
        print("No specific command recognized. Sending query to Gemini for a general response.")
        response = get_gemini_response(query)
        return response

if __name__ == "__main__":
    # The standalone execution block should still use speak() directly
    # for its initial message and final responses, as there's no UI queue.
    chat_history = []
    initial_bot_message = "Hello! How can I help you today?"
    speak(initial_bot_message)
    chat_history.append({"role": "model", "parts": [{"text": initial_bot_message}]})

    # Simple test function to verify TTS engine independently
    def test_tts():
        test_text = "This is a test of the text to speech system."
        print("[DEBUG] Running TTS test...")
        speak(test_text)
        time.sleep(5)  # Wait for speech to complete

    # Run the test
    test_tts()

    while True:
        query = recognize_speech()
        if query:
            task_result = perform_task(query)
            if task_result == "exit_command":
                break
            # In standalone mode, if perform_task returns a non-interrupted response, speak it.
            if task_result and task_result != "interrupted":
                speak(task_result) # Only speak here if running standalone and not interrupted
        else:
            speak("I didn't catch your question. Please try again.")
