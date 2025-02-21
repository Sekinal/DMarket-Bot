# bot/bot.py
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
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
import threading
import random
from rich.logging import RichHandler

# Create 'logs' folder if it doesn't exist
log_directory = "logs"
os.makedirs(log_directory, exist_ok=True)

# Generate a log filename with a timestamp
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = os.path.join(log_directory, f"bot_debug_{timestamp}.log")

# Set up logging configuration to output to a file with a timestamp
log_format = "%(message)s"  # Basic log format without timestamp for pretty output
logging.basicConfig(
    level=logging.DEBUG,  # Use DEBUG level to capture detailed logs
    format=log_format,
    handlers=[
        RichHandler(rich_tracebacks=True),  # RichHandler for console logging with colors
        logging.FileHandler(log_filename)  # Save logs to a file in the 'logs' folder
    ]
)

# Logger with rich handling
logger = logging.getLogger(__name__)
console = Console()

@dataclass
class DMarketConfig:
    public_key: str
    secret_key: str
    api_url: str
    game_id: str
    currency: str = "USD"
    check_interval: int = 960

class RateLimiter:
    def __init__(self, requests_per_second: int, max_retries: int = 5, backoff_factor: float = 2.0):
        self.requests_per_second = requests_per_second
        self.last_request_time = 0
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def wait_if_needed(self):
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time

        if time_since_last_request < 1 / self.requests_per_second:
            sleep_time = 1 / self.requests_per_second - time_since_last_request
            time.sleep(sleep_time)

        self.last_request_time = time.time()

    def handle_rate_limit(self, retries: int = 0):
        # Implement exponential backoff with random jitter
        if retries >= self.max_retries:
            raise RequestException("Maximum retries reached. Rate limit could not be overcome.")

        sleep_time = (2 ** retries) + random.uniform(0, 1)  # Exponential backoff with jitter
        logger.warning(f"Rate limit hit, backing off for {sleep_time:.2f} seconds.")
        time.sleep(sleep_time)  # Wait before retrying

class DMarketAPI:
    def __init__(self, config: DMarketConfig, bot_manager=None):
        self.config = config
        self.rate_limiter = RateLimiter(5)  # Conservative rate limit
        self.session = requests.Session()
        self.bot_manager = bot_manager

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
        retries = 0
        while retries <= self.rate_limiter.max_retries:
            try:
                headers = self._generate_headers(method, path, body)
                url = f"{self.config.api_url}{path}"

                logger.debug(f"Making {method} request to {url} with headers: {headers} and body: {body}")
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body
                )
                response.raise_for_status()  # Raise an exception for HTTP error responses
                logger.debug(f"Received response: {response.status_code} - {response.text}")
                return response.json()

            except RequestException as e:
                if response.status_code == 429:  # Rate-limited (HTTP 429)
                    self.rate_limiter.handle_rate_limit(retries)
                    retries += 1
                    continue
                else:
                    logger.error(f"Request failed: {e} - URL: {url}, Method: {method}, Body: {body}")
                    raise

    def get_current_targets(self) -> Dict[str, Any]:
        logger.info("Fetching current active targets from the marketplace.")
        return self._make_request(
            "GET",
            f"/marketplace-api/v1/user-targets?GameID={self.config.game_id}&BasicFilters.Status=TargetStatusActive"
        )

    def delete_target(self, target_id: str):
        logger.info(f"Deleting target with ID: {target_id}")
        body = {"Targets": [{"TargetID": target_id}]}
        return self._make_request(
            "POST",
            "/marketplace-api/v1/user-targets/delete",
            body
        )

    def create_target(self, title: str, amount: str, price: float, attributes: Dict = None):
        logger.info(f"Creating target: {title} - Price: ${price}, Amount: {amount}, Attributes: {attributes}")
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
        
        response = self._make_request(
            "POST",
            "/marketplace-api/v1/user-targets/create",
            body
        )
        
        logger.info(f"Target created successfully: {response}")
        return response

    def get_market_prices(self, title: str) -> Dict[str, Any]:
        logger.info(f"Fetching market prices for {title}")
        return self._make_request(
            "GET",
            f"/marketplace-api/v1/targets-by-title/{self.config.game_id}/{title}"
        )

class BotInstance:
    def __init__(self, instance_id: str, config: DMarketConfig, bot_manager=None):
        self.instance_id = instance_id
        self.bot_manager = bot_manager
        self.api = DMarketAPI(config, bot_manager)
        self.config = config
        self.console = Console()
        self.running = False
        self.thread = None
        self.first_cycle_complete = False
        self.shutdown_event = threading.Event()

    def start(self):
        if not self.running:
            self.running = True
            self.first_cycle_complete = False
            self.thread = threading.Thread(target=self.run)
            self.thread.start()

    def stop(self):
        self.running = False
        self.shutdown_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)
            
    def run(self):
        self.console.print(Panel.fit(
            "[bold green]DMarket Bot Started[/bold green]\n"
            f"[cyan]API URL:[/cyan] {self.config.api_url}\n"
            f"[cyan]Game ID:[/cyan] {self.config.game_id}\n"
            f"[cyan]Check Interval:[/cyan] {self.config.check_interval} seconds",
            title="Bot Configuration",
            border_style="blue"
        ))

        while self.running:
            try:
                with self.console.status("[bold green]Fetching current targets...") as status:
                    current_targets = self.api.get_current_targets()

                # Extract and report items to bot manager
                items = [target["Title"] for target in current_targets.get("Items", [])]
                if self.bot_manager:
                    self.bot_manager.update_available_items(items)

                self.console.print(f"\n[bold]Found {len(current_targets.get('Items', []))} active targets[/bold]")

                for target in current_targets.get("Items", []):
                    self.console.rule(f"[bold blue]Processing Target: {target['Title']}[/bold blue]")
                    self.update_target(
                        target["Title"],
                        float(target["Price"]["Amount"]),
                        target
                    )

                if not self.first_cycle_complete:
                    self.first_cycle_complete = True
                    self.console.print("\n[bold green]First cycle completed - full updates will start from next cycle[/bold green]")

                self.console.print(f"\n[yellow]Waiting {self.config.check_interval} seconds before next update...[/yellow]")
                self.shutdown_event.wait(timeout=self.config.check_interval)
                if self.shutdown_event.is_set():
                    break

            except Exception as e:
                logger.error(f"Error in main loop: {str(e)}", exc_info=True)
                self.console.print(f"[bold red]Error in main loop:[/bold red] {str(e)}", style="red")
                self.shutdown_event.wait(timeout=self.config.check_interval)
                if self.shutdown_event.is_set():
                    break
                
    def print_target_info(self, title: str, current_price: float, attributes: Dict):
        panel = Panel(
            f"[cyan]Title:[/cyan] {title}\n"
            f"[cyan]Current Price:[/cyan] ${current_price:.2f}\n"
            f"[cyan]Attributes:[/cyan] {attributes}",
            title=f"[bold green]{self.instance_id} - Target Information[/bold green]",
            border_style="green"
        )
        self.console.print(panel)
        logger.info(f"[{self.instance_id}] Printed target info for {title} - Price: ${current_price:.2f}")

    def print_market_analysis(self, title: str, highest_price: float, optimal_price: float, current_price: float):
        table = Table(title=f"[bold]{self.instance_id} - Market Analysis for {title}[/bold]", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        
        table.add_row("Current Price", f"${current_price:.2f}")
        table.add_row("Highest Competitor", f"${highest_price:.2f}")
        table.add_row("Optimal Price", f"${optimal_price:.2f}")
        table.add_row("Price Difference", f"${(optimal_price - current_price):.2f}")
        
        self.console.print(table)
        logger.info(f"[{self.instance_id}] Market analysis for {title} - Optimal Price: ${optimal_price:.2f}")

        
        self.console.print(table)

    def print_action_result(self, action: str, details: str):
        self.console.print(f"[bold blue]{self.instance_id} - {action}:[/bold blue] [green]{details}[/green]")
        logger.info(f"[{self.instance_id}] {action}: {details}")


    def update_target(self, title: str, current_price: float, current_target: Dict):
        retries = 0
        max_retries = 5  # Maximum number of retries
        backoff_factor = 2  # Exponential backoff factor
        
        try:
            self.print_target_info(
                title,
                current_price,
                {attr["Name"]: attr["Value"] for attr in current_target["Attributes"]}
            )

            self.console.print(f"[red][{self.instance_id}] Updating target for {title} - Current Price: ${current_price:.2f}[/red]")
            logger.info(f"[{self.instance_id}] Updating target for {title} - Current Price: ${current_price:.2f}")

            target_attrs = {a["Name"]: a["Value"] for a in current_target["Attributes"]}
            phase = target_attrs.get("phase", "")
            float_val = target_attrs.get("floatPartValue", "")
            seed = target_attrs.get("paintSeed", "")

            if self.bot_manager:
                max_price = self.bot_manager.get_max_price(
                    title, phase, float_val, seed
                )
            else:
                max_price = float('inf')

            if self.first_cycle_complete:
                retries = 0
                while retries <= max_retries:
                    try:
                        # Attempt to delete the target with retries
                        self.api.delete_target(current_target["TargetID"])
                        self.console.print(f"[{self.instance_id}] Deleted target: {current_target['TargetID']}")
                        logger.info(f"[{self.instance_id}] Deleted target: {current_target['TargetID']}")
                        break  # Exit the retry loop if successful
                    except Exception as e:
                        retries += 1
                        if retries > max_retries:
                            self.console.print(f"[bold red]Failed to delete target after {max_retries} retries[/bold red]", style="red")
                            logger.error(f"[{self.instance_id}] Failed to delete target after {max_retries} retries", exc_info=True)
                            break
                        else:
                            backoff_time = (2 ** retries) + random.uniform(0, 1)
                            self.console.print(f"[bold yellow]Retrying target deletion in {backoff_time:.2f} seconds...[/bold yellow]", style="yellow")
                            time.sleep(backoff_time)

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
            with self.console.status(f"[bold green]Analyzing orders for {self.instance_id}...") as status:
                for order in market_prices["orders"]:
                    order_attributes = order["attributes"]
                    attributes_match = True
                    if (target_attributes.get("floatPartValue", "any") != order_attributes.get("floatPartValue", "any")
                        and order_attributes.get("floatPartValue", "any") != "any"):
                        attributes_match = False
                    if target_attributes.get("paintSeed", "any") != order_attributes.get("paintSeed", "any"):
                        attributes_match = False
                    if target_attributes.get("phase", "any") != order_attributes.get("phase", "any"):
                        attributes_match = False
                    if attributes_match:
                        relevant_orders.append(order)

            if not relevant_orders:
                self.console.print(f"[red]No orders found for {title}[/red]")
                
                # Retry logic for recreating the target
                retries = 0
                while retries <= max_retries:
                    try:
                        self.console.print(f"[yellow]Recreating target for {self.instance_id}...[/yellow]")
                        self.api.create_target(
                            title=title,
                            amount=current_target["Amount"],
                            price=current_price,
                            attributes=current_target["Attributes"]
                        )
                        break  # Exit the retry loop if successful
                    except Exception as e:
                        retries += 1
                        if retries > max_retries:
                            self.console.print(f"[bold red]Failed to recreate target after {max_retries} retries[/bold red]", style="red")
                            logger.error(f"[{self.instance_id}] Failed to recreate target after {max_retries} retries", exc_info=True)
                            break
                        else:
                            backoff_time = (2 ** retries) + random.uniform(0, 1)
                            self.console.print(f"[bold yellow]Retrying target recreation in {backoff_time:.2f} seconds...[/bold yellow]", style="yellow")
                            time.sleep(backoff_time)
                return 

            highest_price = max(float(order["price"]) / 100 for order in relevant_orders)
            optimal_price = highest_price + 0.01

            self.print_market_analysis(title, highest_price, optimal_price, current_price)

            if abs(current_price - optimal_price) >= 0:
                if optimal_price > max_price:
                    self.console.print(f"\n[red]Optimal price ${optimal_price:.2f} exceeds max price ${max_price:.2f}[/red]")
                    optimal_price = max_price
                if self.first_cycle_complete:
                    self.print_action_result(
                        "Creating new target",
                        f"Price: ${optimal_price:.2f}, Attributes: {current_target['Attributes']}"
                    )
                    # Adding delay before creating the new target to avoid hitting the rate limit
                    self.console.print(f"[yellow]Waiting before creating new target for {self.instance_id}...[/yellow]")
                    time.sleep(1)  # Adjust delay if needed to avoid rate-limiting
                    
                    # Retry logic for creating the target
                    retries = 0
                    while retries <= max_retries:
                        try:
                            self.api.create_target(
                                title=title,
                                amount=current_target["Amount"],
                                price=optimal_price,
                                attributes=current_target["Attributes"]
                            )
                            break  # Exit the retry loop if successful
                        except Exception as e:
                            retries += 1
                            if retries > max_retries:
                                self.console.print(f"[bold red]Failed to create target after {max_retries} retries[/bold red]", style="red")
                                logger.error(f"[{self.instance_id}] Failed to create target after {max_retries} retries", exc_info=True)
                                break
                            else:
                                backoff_time = (2 ** retries) + random.uniform(0, 1)
                                self.console.print(f"[bold yellow]Retrying target creation in {backoff_time:.2f} seconds...[/bold yellow]", style="yellow")
                                time.sleep(backoff_time)
                else:
                    self.console.print(f"\n[red]SKIPPING PRICE UPDATE FOR {self.instance_id}[/red]")
            else:
                self.console.print("\n[green]Price is already optimal[/green]")

        except Exception as e:
            logger.error(f"[{self.instance_id}] Error updating target: {str(e)}", exc_info=True)
            self.console.print(f"[bold red]Error updating target for {self.instance_id}:[/bold red] {str(e)}", style="red")

class BotManager:
    def __init__(self):
        self.bots = {}
        self.config_file = "config/bots_config.json"
        self.max_prices_file = "config/max_prices.json"
        self.max_prices = {}
        self.available_items = set()
        self.load_configs()
        self.load_max_prices()
        self.load_existing_items()
        
    def load_existing_items(self):
        try:
            with open('config/max_prices.json', 'r') as f:
                data = json.load(f)
                for price_entry in data:
                    self.available_items.add(price_entry['item'])
        except FileNotFoundError:
            pass
        
    def load_max_prices(self):
        try:
            with open(self.max_prices_file, 'r') as f:
                self.max_prices = json.load(f)
        except FileNotFoundError:
            self.max_prices = []
            self.save_max_prices()

    def save_max_prices(self):
        os.makedirs('config', exist_ok=True)
        with open(self.max_prices_file, 'w') as f:
            json.dump(self.max_prices, f, indent=4)

    def update_max_price(self, item_name: str, phase: str, float_val: str, seed: str, max_price: float):
        # Remove existing entry if exists
        self.max_prices = [entry for entry in self.max_prices if not (
            entry['item'] == item_name and
            entry.get('phase', '') == phase and
            entry.get('float', '') == float_val and
            entry.get('seed', '') == seed
        )]
        # Add new entry
        self.max_prices.append({
            'item': item_name,
            'phase': phase,
            'float': float_val,
            'seed': seed,
            'price': max_price
        })
        self.save_max_prices()

    def get_max_price(self, item_name: str, phase: str, float_val: str, seed: str) -> float:
        matching = []
        for entry in self.max_prices:
            if entry['item'] != item_name:
                continue
            match = True
            if entry.get('phase', '') and entry['phase'] != phase:
                match = False
            if entry.get('float', '') and entry['float'] != float_val:
                match = False
            if entry.get('seed', '') and entry['seed'] != seed:
                match = False
            if match:
                matching.append(entry)
        
        if not matching:
            return float('inf')
        
        # Find most specific entry (most attributes specified)
        best_entry = max(matching, key=lambda x: sum(1 for k in ['phase', 'float', 'seed'] if x.get(k, '')))
        return best_entry['price']

    def update_available_items(self, items: list):
        self.available_items = set(items)

    def load_configs(self):
        try:
            with open(self.config_file, 'r') as f:
                configs = json.load(f)
                for instance_id, config_data in configs.items():
                    if instance_id not in self.bots:
                        config = DMarketConfig(
                            public_key=config_data['public_key'],
                            secret_key=config_data['secret_key'],
                            api_url=config_data.get('api_url', "https://api.dmarket.com"),
                            game_id=config_data.get('game_id', "a8db"),
                            currency=config_data.get('currency', "USD"),
                            check_interval=config_data.get('check_interval', 960)
                        )
                        self.bots[instance_id] = BotInstance(instance_id, config, self)
        except FileNotFoundError:
            logger.warning("No config file found. Creating empty configuration.")
            self.save_configs()

    def save_configs(self):
        configs = {
            instance_id: {
                'public_key': bot.config.public_key,
                'secret_key': bot.config.secret_key,
                'api_url': bot.config.api_url,
                'game_id': bot.config.game_id,
                'currency': bot.config.currency,
                'check_interval': bot.config.check_interval
            }
            for instance_id, bot in self.bots.items()
        }
        os.makedirs('config', exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(configs, f, indent=4)

    def add_bot(self, instance_id: str, config: DMarketConfig):
        if instance_id not in self.bots:
            self.bots[instance_id] = BotInstance(instance_id, config, self)
            self.save_configs()
            return True
        return False

    def remove_bot(self, instance_id: str):
        if instance_id in self.bots:
            try:
                self.bots[instance_id].stop()
                del self.bots[instance_id]
                self.save_configs()
                return True
            except Exception as e:
                logger.error(f"Error removing bot {instance_id}: {e}")
                return False
        return False

    def start_bot(self, instance_id: str):
        if instance_id in self.bots:
            self.bots[instance_id].start()
            return True
        return False

    def stop_bot(self, instance_id: str):
        if instance_id in self.bots:
            self.bots[instance_id].stop()
            return True
        return False

    def get_bot_status(self, instance_id: str):
        if instance_id in self.bots:
            return {
                'running': self.bots[instance_id].running,
                'config': vars(self.bots[instance_id].config)
            }
        return None

    def get_all_bots(self):
        return {
            instance_id: {
                'running': bot.running,
                'config': vars(bot.config)
            }
            for instance_id, bot in self.bots.items()
        }