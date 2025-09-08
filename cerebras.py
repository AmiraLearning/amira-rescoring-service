import time

import requests

# Your OpenRouter API key
API_KEY = "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"


def test_cerebras_speed():
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "Cerebras Speed Test",
    }

    data = {
        "model": "qwen/qwen3-coder",
        "messages": [{"role": "user", "content": "Write a 2000-word essay about the future of AI"}],
        "stream": False,
        "provider": {"only": ["Cerebras"], "require_parameters": True},
    }

    start_time = time.time()

    print("Making request to Cerebras...")
    response = requests.post(url, headers=headers, json=data, stream=True)

    print(f"Response status: {response.status_code}")
    print(f"Response headers: {dict(response.headers)}")

    if response.status_code != 200:
        print(f"Error response: {response.text}")
        return

    end_time = time.time()
    duration = end_time - start_time

    print(f"Response: {response.text}")
    word_count = len(response.text.split())

    print("\n\n--- Performance Stats ---")
    print(f"Time: {duration:.2f} seconds")
    print(f"Word count: {word_count}")
    token_count = word_count * 1.3
    print(f"Approximate tokens: {token_count}")
    print(f"Tokens/second: {token_count / duration:.0f}" if duration > 0 else "N/A")


if __name__ == "__main__":
    test_cerebras_speed()
