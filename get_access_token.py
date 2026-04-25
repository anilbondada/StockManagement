import hashlib
import webbrowser
import requests
API_KEY      = "kb7ux8tgqc6kek6c"
API_SECRET   = "nncu2lwokkz206tjz863ci6uka4voucs"

BASE_URL = "https://api.kite.trade"


def get_login_url():
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={API_KEY}"


def calculate_checksum(request_token):
    data = API_KEY + request_token + API_SECRET
    return hashlib.sha256(data.encode()).hexdigest()


def get_access_token(request_token):
    checksum = calculate_checksum(request_token)
    response = requests.post(
        f"{BASE_URL}/session/token",
        data={
            "api_key": API_KEY,
            "request_token": request_token,
            "checksum": checksum,
        },
        headers={"X-Kite-Version": "3"},
    )
    response.raise_for_status()
    data = response.json()["data"]
    return data["access_token"]


if __name__ == "__main__":
    login_url = get_login_url()
    print(f"Opening login URL:\n{login_url}\n")
    webbrowser.open(login_url)

    request_token = input(
        "After login, paste the 'request_token' from the redirect URL: "
    ).strip()

    access_token = get_access_token(request_token)
    print(f"\nAccess Token: {access_token}")
    print("\nUse this header for subsequent API calls:")
    print(f"Authorization: token {API_KEY}:{access_token}")
