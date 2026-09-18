from .base import ScrapeResult, PLATFORMS
from .perplexity import scrape as scrape_perplexity
from .chatgpt import scrape as scrape_chatgpt
from .gemini import scrape as scrape_gemini
from .google import scrape as scrape_google

SCRAPERS = {
    "perplexity": scrape_perplexity,
    "chatgpt": scrape_chatgpt,
    "gemini": scrape_gemini,
    "google": scrape_google,
}
