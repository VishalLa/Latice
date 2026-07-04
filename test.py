import os
import redis
from dotenv import load_dotenv

def nuke_redis():
    # Load your existing .env file to grab the REDIS_URL
    load_dotenv()
    
    # Default to localhost if the variable is missing for some reason
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    
    print(f"📍 Attempting to connect to Redis at: {redis_url}")
    
    try:
        # Connect to the Redis instance
        client = redis.from_url(redis_url)
        
        # Ping the server to verify the connection is active
        client.ping()
        print("✅ Connection successful! Preparing to flush...")
        
        # flushall() clears ALL keys across ALL databases in this Redis instance
        client.flushall()
        
        print("💥 KABOOM! Redis has been completely flushed.")
        print("👻 Your ghost tasks and cached data are officially gone.")
        
    except redis.exceptions.ConnectionError:
        print("❌ ERROR: Could not connect to Redis. Make sure your Redis server is actually running!")
    except Exception as e:
        print(f"❌ ERROR: An unexpected issue occurred: {e}")

if __name__ == "__main__":
    nuke_redis()
