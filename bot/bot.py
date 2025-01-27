import os
import json
import time
from datetime import datetime
from typing import Dict, Any
import logging
from dataclasses import dataclass
import requests
from nacl.bindings import crypto_sign
from requests.exceptions import RequestException
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class DMarketConfig:
    public_key: str
    secret_key: str
    api_url: str
    game_id: str
    currency: str = "USD"
    check_interval: int = 30  # seconds

class RateLimiter:
    def __init__(self, requests_per_second: int):
        self.requests_per_second = requests_per_second
        self.last_request_time = 0

    def wait_if_needed(self):
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time
        if time_since_last_request < 1/self.requests_per_second:
            time.sleep(1/self.requests_per_second - time_since_last_request)
        self.last_request_time = time.time()

class DMarketAPI:
    def __init__(self, config: DMarketConfig):
        self.config = config
        self.rate_limiter = RateLimiter(5)  # Conservative rate limit
        self.session = requests.Session()

    def _generate_headers(self, method: str, path: str, body: Dict = None) -> Dict[str, str]:
        nonce = str(round(datetime.now().timestamp()))
        string_to_sign = method + path
        if body:
            string_to_sign += json.dumps(body)
        string_to_sign += nonce

        signature_bytes = crypto_sign(
            string_to_sign.encode('utf-8'),
            bytes.fromhex(self.config.secret_key)
        )
        signature = signature_bytes[:64].hex()

        return {
            "X-Api-Key": self.config.public_key,
            "X-Request-Sign": f"dmar ed25519 {signature}",
            "X-Sign-Date": nonce,
            "Content-Type": "application/json"
        }

    def _make_request(self, method: str, path: str, body: Dict = None) -> Dict[Any, Any]:
        self.rate_limiter.wait_if_needed()
        
        try:
            headers = self._generate_headers(method, path, body)
            url = f"{self.config.api_url}{path}"

            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                json=body
            )
            
            response.raise_for_status()
            return response.json()

        except RequestException as e:
            logger.error(f"Request failed: {e}")
            raise

    def get_current_targets(self) -> Dict[str, Any]:
        return self._make_request(
            "GET",
            f"/marketplace-api/v1/user-targets?GameID={self.config.game_id}"
        )

    def delete_target(self, target_id: str):
        body = {"Targets": [{"TargetID": target_id}]}
        return self._make_request(
            "POST",
            "/marketplace-api/v1/user-targets/delete",
            body
        )

    def create_target(self, title: str, amount: str, price: float):
        body = {
            "GameID": self.config.game_id,
            "Targets": [{
                "Amount": amount,
                "Price": {
                    "Currency": self.config.currency,
                    "Amount": price
                },
                "Title": title
            }]
        }
        return self._make_request(
            "POST",
            "/marketplace-api/v1/user-targets/create",
            body
        )

    def get_market_prices(self, title: str) -> Dict[str, Any]:
        return self._make_request(
            "GET",
            f"/marketplace-api/v1/targets-by-title/{self.config.game_id}/{title}"
        )

class DMarketBot:
    def __init__(self, config: DMarketConfig):
        self.api = DMarketAPI(config)
        self.config = config

    def update_target(self, title: str, current_price: float):
        try:
            # Get current market prices
            market_prices = self.api.get_market_prices(title)
            if not market_prices.get("orders"):
                logger.info(f"No orders found for {title}")
                return

            # Find highest purchase target
            highest_price = max(
                float(order["price"]) 
                for order in market_prices["orders"]
            )

            # If our price isn't highest, update it
            if current_price <= highest_price:
                new_price = highest_price + 0.01
                logger.info(f"Updating price for {title} from {current_price} to {new_price}")
                
                # Get our current targets
                current_targets = self.api.get_current_targets()
                
                # Delete old target
                for target in current_targets.get("Items", []):
                    if target["Title"] == title:
                        self.api.delete_target(target["TargetID"])
                
                # Create new target
                self.api.create_target(title, "1", new_price)

        except Exception as e:
            logger.error(f"Error updating target: {e}")

    def run(self):
        logger.info("Starting DMarket bot...")
        
        while True:
            try:
                current_targets = self.api.get_current_targets()
                
                for target in current_targets.get("Items", []):
                    self.update_target(
                        target["Title"],
                        float(target["Price"]["Amount"])
                    )

                time.sleep(self.config.check_interval)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(self.config.check_interval)

if __name__ == "__main__":
    load_dotenv()
    
    config = DMarketConfig(
        public_key=os.getenv("PUBLIC_KEY"),
        secret_key=os.getenv("SECRET_KEY"),
        api_url="https://api.dmarket.com",
        game_id="a8db"  # CS:GO
    )

    bot = DMarketBot(config)
    bot.run()