import pandas as pd
from kiteconnect import KiteConnect
from datetime import datetime, timedelta
import pytz
import webbrowser
import time

# Initialize Kite Connect
# Step 1: Initialize KiteConnect with your API key
api_key = "kb7ux8tgqc6kek6c"
api_secret = "nncu2lwokkz206tjz863ci6uka4voucs"

kite = KiteConnect(api_key=api_key)
#kite.set_access_token(access_token)

# Step 2: Get the login URL and open it in browser
login_url = kite.login_url()
print(f"Login URL: {login_url}")
webbrowser.open(login_url)

# Step 3: After manual login, you'll be redirected to your redirect_url
# The URL will contain the request_token parameter
# Example: http://your-redirect-url.com/?request_token=abc123xyz&action=login&status=success

# Extract the request_token from the redirected URL
request_token = input("Enter the request_token from the URL: ")

# Step 4: Generate session and get access_token
data = kite.generate_session(request_token, api_secret=api_secret)
access_token = data["access_token"]
public_token = data["public_token"]
user_id = data["user_id"]

print(f"Access Token: {access_token}")
print(f"Public Token: {public_token}")
print(f"User ID: {user_id}")

# Step 5: Set the access token for subsequent API calls
kite.set_access_token(access_token)

# Save the access token to a file for reuse (valid for the trading day)
with open('access_token.txt', 'w') as f:
    f.write(access_token)

print("Access token saved to access_token.txt")

# Now you can make API calls
profile = kite.profile()
print(f"User Profile: {profile}")

# Read the input file
df = pd.read_excel('Backtest-EarlyStockInPlay.xlsx')

# Initialize result storage
results = []

# Get all instruments once to avoid repeated API calls
instruments = kite.instruments("NSE")
instruments_df = pd.DataFrame(instruments)

for idx, row in df.iterrows():
    symbol = row['symbol']
    target_date = pd.to_datetime(row['date'])

    print(f"\nProcessing: {symbol} for date {target_date.date()}")

    try:
        # 1. Get NSE equity instrument
        instrument = instruments_df[
            (instruments_df['tradingsymbol'] == symbol) &
            (instruments_df['exchange'] == 'NSE') &
            (instruments_df['instrument_type'] == 'EQ')
            ]

        if instrument.empty:
            print(f"  Warning: Instrument {symbol} not found")
            continue

        instrument_token = instrument.iloc[0]['instrument_token']
        print(f"  Instrument Token: {instrument_token}")

        # 2. Get daily OHLCV for the listed date
        from_date = target_date.date()
        to_date = target_date.date()

        daily_data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval="day"
        )

        if not daily_data:
            print(f"  Warning: No daily data found for {symbol} on {target_date.date()}")
            daily_ohlcv = {
                'daily_open': None, 'daily_high': None,
                'daily_low': None, 'daily_close': None, 'daily_volume': None
            }
        else:
            daily_candle = daily_data[0]
            daily_ohlcv = {
                'daily_open': daily_candle['open'],
                'daily_high': daily_candle['high'],
                'daily_low': daily_candle['low'],
                'daily_close': daily_candle['close'],
                'daily_volume': daily_candle['volume']
            }
            print(f"  Daily OHLCV: O={daily_ohlcv['daily_open']}, H={daily_ohlcv['daily_high']}, "
                  f"L={daily_ohlcv['daily_low']}, C={daily_ohlcv['daily_close']}, V={daily_ohlcv['daily_volume']}")

        # 3. Get first three 5-minute candles
        # Market opens at 9:15 AM, so first three 5-min candles are:
        # 9:15-9:20, 9:20-9:25, 9:25-9:30
        five_min_data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval="5minute"
        )

        if not five_min_data or len(five_min_data) < 3:
            print(f"  Warning: Insufficient 5-min candle data for {symbol}")
            first_three_candles = []
        else:
            first_three_candles = five_min_data[:3]
            print(f"  First 3 candles extracted")

        # Store results
        result_row = {
            'date': target_date,
            'symbol': symbol,
            'instrument_token': instrument_token,
            'exchange': 'NSE',
            **daily_ohlcv
        }

        # Add 5-min candle data
        for i, candle in enumerate(first_three_candles, 1):
            #result_row[f'candle{i}_time'] = candle['date']
            result_row[f'candle{i}_open'] = candle['open']
            result_row[f'candle{i}_high'] = candle['high']
            result_row[f'candle{i}_low'] = candle['low']
            result_row[f'candle{i}_close'] = candle['close']
            result_row[f'candle{i}_volume'] = candle['volume']

        results.append(result_row)

        # Rate limiting - sleep to avoid hitting API limits
        time.sleep(0.5)

    except Exception as e:
        print(f"  Error processing {symbol}: {str(e)}")
        continue

# Create output dataframe
output_df = pd.DataFrame(results)

# Save to Excel
output_df.to_excel('kite_ohlcv_data.xlsx', index=False)
print(f"\n✓ Data extraction complete. Output saved to 'kite_ohlcv_data.xlsx'")
print(f"Total records processed: {len(output_df)}")

# Display sample
print("\nSample output:")
print(output_df.head())