import requests
import time
import json
import hmac
import hashlib
from datetime import datetime
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
import asyncio
from aiohttp import ClientSession, ClientTimeout
from tenacity import retry, wait_exponential, stop_after_attempt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    PUBLIC_KEY: str
    SECRET_KEY: str
    BASE_URL: str = "https://api.dmarket.com"
    CHECK_INTERVAL: int = 5  # seconds
    CSGO_GAME_ID: str = "a8db"

class RateLimiter:
    def __init__(self):
        self.limits: Dict[str, Dict] = {
            "default": {"limit": 20, "remaining": 20, "reset_at": 0},
            "market_items": {"limit": 10, "remaining": 10, "reset_at": 0},
            "last_sales": {"limit": 6, "remaining": 6, "reset_at": 0}
        }

    async def update_limits(self, headers: Dict, endpoint_type: str = "default"):
        remaining = headers.get("RateLimit-Remaining")
        if remaining is not None:
            self.limits[endpoint_type]["remaining"] = int(remaining)
            self.limits[endpoint_type]["reset_at"] = time.time() + int(headers.get("RateLimit-Reset", 1))

    async def wait_if_needed(self, endpoint_type: str = "default"):
        limit_info = self.limits[endpoint_type]
        if limit_info["remaining"] <= 1:
            wait_time = max(0, limit_info["reset_at"] - time.time())
            if wait_time > 0:
                logger.info(f"Rate limit reached for {endpoint_type}, waiting {wait_time:.2f} seconds")
                await asyncio.sleep(wait_time)

class DMarketAPI:
    def __init__(self, config: Config):
        self.config = config
        self.rate_limiter = RateLimiter()
        self.session = ClientSession(timeout=ClientTimeout(total=30))

    def _generate_signature(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        string_to_sign = method + path + body + timestamp
        
        signature = hmac.new(
            bytes.fromhex(self.config.SECRET_KEY),
            string_to_sign.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "X-Api-Key": self.config.PUBLIC_KEY,
            "X-Request-Sign": signature,
            "X-Sign-Date": timestamp
        }

    @retry(wait=wait_exponential(multiplier=1, min=4, max=10),
           stop=stop_after_attempt(3))
    async def _make_request(self, method: str, endpoint: str, params: Dict = None, data: Optional[Dict] = None) -> Dict:
        url = f"{self.config.BASE_URL}{endpoint}"
        headers = self._generate_signature(method, endpoint, json.dumps(data) if data else "")
        
        await self.rate_limiter.wait_if_needed()
        
        async with getattr(self.session, method.lower())(url, params=params, json=data, headers=headers) as response:
            await self.rate_limiter.update_limits(response.headers)
            
            if response.status >= 400:
                logger.error(f"Request failed: {response.status} - {await response.text()}")
                response.raise_for_status()
            
            return await response.json()

    async def get_active_targets(self) -> List[Dict]:
        """Get all active CS:GO buy orders (targets)"""
        params = {
            "GameID": self.config.CSGO_GAME_ID,
            "BasicFilters.Status": "TargetStatusActive"
        }
        response = await self._make_request("GET", "/marketplace-api/v1/user-targets", params=params)
        return response.get("Items", [])

    async def get_market_offers(self, title: str) -> List[Dict]:
        params = {
            "title": title,
            "gameId": self.config.CSGO_GAME_ID,
            "currency": "USD"
        }
        response = await self._make_request("GET", "/exchange/v1/offers-by-title", params=params)
        return response.get("objects", [])

    async def delete_target(self, target_id: str) -> bool:
        data = {"Targets": [{"TargetID": target_id}]}
        response = await self._make_request("POST", "/marketplace-api/v1/user-targets/delete", data)
        return response.get("Result", [{}])[0].get("Successful", False)

    async def create_target(self, title: str, price: float, amount: str = "1") -> Optional[str]:
        data = {
            "GameID": self.config.CSGO_GAME_ID,
            "Targets": [{
                "Amount": amount,
                "Price": {"Currency": "USD", "Amount": price},
                "Title": title
            }]
        }
        response = await self._make_request("POST", "/marketplace-api/v1/user-targets/create", data)
        result = response.get("Result", [{}])[0]
        return result.get("TargetID") if result.get("Successful") else None

class PurchaseOrderBot:
    def __init__(self, config: Config):
        self.api = DMarketAPI(config)
        self.monitored_items: Dict[str, Dict] = {}
        self.config = config

    async def load_existing_targets(self) -> None:
        """Load all active CS:GO buy orders from DMarket"""
        try:
            active_targets = await self.api.get_active_targets()
            
            for target in active_targets:
                title = target["Title"]
                self.monitored_items[title] = {
                    "target_id": target["TargetID"],
                    "current_price": float(target["Price"]["Amount"]),
                    "amount": target["Amount"]
                }
                logger.info(f"Loaded existing target: {title} at ${float(target['Price']['Amount']):.2f}")
            
            logger.info(f"Loaded {len(self.monitored_items)} active CS:GO buy orders")
            
        except Exception as e:
            logger.error(f"Error loading existing targets: {str(e)}")
            raise

    async def update_purchase_order(self, title: str, item_info: Dict) -> None:
        try:
            # Get current market offers
            market_offers = await self.api.get_market_offers(title)
            if not market_offers:
                logger.warning(f"No market offers found for {title}")
                return

            # Find the highest purchase order
            highest_offer = max(market_offers, key=lambda x: float(x["price"]["USD"]))
            highest_price = float(highest_offer["price"]["USD"])
            current_price = item_info["current_price"]

            if current_price <= highest_price:
                # Delete current target and create new one
                if await self.api.delete_target(item_info["target_id"]):
                    new_price = highest_price + 0.01
                    new_target_id = await self.api.create_target(
                        title, 
                        new_price,
                        amount=item_info["amount"]
                    )
                    
                    if new_target_id:
                        self.monitored_items[title].update({
                            "target_id": new_target_id,
                            "current_price": new_price
                        })
                        logger.info(f"Updated purchase order for {title}: ${new_price:.2f}")
                    else:
                        logger.error(f"Failed to create new target for {title}")

        except Exception as e:
            logger.error(f"Error updating purchase order for {title}: {str(e)}")

    async def run(self):
        try:
            # Initial load of existing targets
            await self.load_existing_targets()
            
            # Main loop
            while True:
                for title, item_info in self.monitored_items.items():
                    await self.update_purchase_order(title, item_info)
                    # Small delay between items to respect rate limits
                    await asyncio.sleep(0.5)
                
                # Reload targets periodically to catch any manual changes
                if len(self.monitored_items) > 0:
                    await self.load_existing_targets()
                
                await asyncio.sleep(self.config.CHECK_INTERVAL)
                
        except Exception as e:
            logger.error(f"Error in main loop: {str(e)}")
            # Wait before retrying
            await asyncio.sleep(self.config.CHECK_INTERVAL)
            await self.run()  # Restart the main loop

async def main():
    config = Config(
        PUBLIC_KEY="your_public_key",
        SECRET_KEY="your_secret_key"
    )
    
    bot = PurchaseOrderBot(config)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot stopped due to error: {str(e)}")
    finally:
        # Cleanup
        await bot.api.session.close()

if __name__ == "__main__":
    asyncio.run(main())