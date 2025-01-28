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
from rich import print
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich import box

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
console = Console()

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
            f"/marketplace-api/v1/user-targets?GameID={self.config.game_id}&BasicFilters.Status=TargetStatusActive"
        )

    def delete_target(self, target_id: str):
        body = {"Targets": [{"TargetID": target_id}]}
        return self._make_request(
            "POST",
            "/marketplace-api/v1/user-targets/delete",
            body
        )

    def create_target(self, title: str, amount: str, price: float, attributes: Dict = None):
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
        if attributes:
            attrs = {}
            for attr in attributes:
                if attr["Name"] in ["paintSeed", "phase", "floatPartValue"]:
                    attrs[attr["Name"]] = attr["Value"]
            if attrs:
                body["Targets"][0]["Attrs"] = attrs
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
        self.console = Console()

    def print_target_info(self, title: str, current_price: float, attributes: Dict):
        panel = Panel(
            f"[cyan]Title:[/cyan] {title}\n"
            f"[cyan]Current Price:[/cyan] ${current_price:.2f}\n"
            f"[cyan]Attributes:[/cyan] {attributes}",
            title="[bold green]Target Information[/bold green]",
            border_style="green"
        )
        self.console.print(panel)

    def print_market_analysis(self, title: str, highest_price: float, optimal_price: float, current_price: float):
        table = Table(title=f"[bold]Market Analysis for {title}[/bold]", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        
        table.add_row("Current Price", f"${current_price:.2f}")
        table.add_row("Highest Competitor", f"${highest_price:.2f}")
        table.add_row("Optimal Price", f"${optimal_price:.2f}")
        table.add_row("Price Difference", f"${(optimal_price - current_price):.2f}")
        
        self.console.print(table)

    def print_action_result(self, action: str, details: str):
        self.console.print(f"[bold blue]{action}:[/bold blue] [green]{details}[/green]")

    def update_target(self, title: str, current_price: float, current_target: Dict):
        try:
            self.print_target_info(
                title, 
                current_price, 
                {attr["Name"]: attr["Value"] for attr in current_target["Attributes"]}
            )

            self.console.print("[yellow]Fetching market prices...[/yellow]")
            market_prices = self.api.get_market_prices(title)
            
            if not market_prices.get("orders"):
                self.console.print(f"[red]No orders found for {title}[/red]")
                return

            relevant_orders = []
            target_attributes = {
                attr["Name"]: attr["Value"]
                for attr in current_target["Attributes"]
            }

            with self.console.status("[bold green]Analyzing orders...") as status:
                for order in market_prices["orders"]:
                    order_attributes = order["attributes"]
                    attributes_match = True
                    if target_attributes.get("paintSeed", "any") != order_attributes.get("paintSeed", "any"):
                        attributes_match = False
                    if target_attributes.get("phase", "any") != order_attributes.get("phase", "any"):
                        attributes_match = False
                    if attributes_match:
                        relevant_orders.append(order)

            if not relevant_orders:
                self.console.print(f"[red]No matching orders found for {title} with specific attributes[/red]")
                return

            highest_price = max(float(order["price"]) / 100 for order in relevant_orders)
            optimal_price = highest_price + 0.01

            self.print_market_analysis(title, highest_price, optimal_price, current_price)

            if abs(current_price - optimal_price) > 0.01:
                self.console.print("\n[yellow]Price adjustment needed[/yellow]")
                
                self.print_action_result("Deleting old target", f"ID: {current_target['TargetID']}")
                self.api.delete_target(current_target["TargetID"])

                self.print_action_result(
                    "Creating new target",
                    f"Price: ${optimal_price:.2f}, Attributes: {current_target['Attributes']}"
                )
                self.api.create_target(
                    title=title,
                    amount=current_target["Amount"],
                    price=optimal_price,
                    attributes=current_target["Attributes"]
                )
            else:
                self.console.print("\n[green]Price is already optimal[/green]")

        except Exception as e:
            self.console.print(f"[bold red]Error updating target:[/bold red] {str(e)}", style="red")

    def run(self):
        self.console.print(Panel.fit(
            "[bold green]DMarket Bot Started[/bold green]\n"
            f"[cyan]API URL:[/cyan] {self.config.api_url}\n"
            f"[cyan]Game ID:[/cyan] {self.config.game_id}\n"
            f"[cyan]Check Interval:[/cyan] {self.config.check_interval} seconds",
            title="Bot Configuration",
            border_style="blue"
        ))

        while True:
            try:
                with self.console.status("[bold green]Fetching current targets...") as status:
                    current_targets = self.api.get_current_targets()
                
                self.console.print(f"\n[bold]Found {len(current_targets.get('Items', []))} active targets[/bold]")
                
                for target in current_targets.get("Items", []):
                    self.console.rule(f"[bold blue]Processing Target: {target['Title']}[/bold blue]")
                    self.update_target(
                        target["Title"],
                        float(target["Price"]["Amount"]),
                        target
                    )

                self.console.print(f"\n[yellow]Waiting {self.config.check_interval} seconds before next update...[/yellow]")
                time.sleep(self.config.check_interval)

            except Exception as e:
                self.console.print(f"[bold red]Error in main loop:[/bold red] {str(e)}", style="red")
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