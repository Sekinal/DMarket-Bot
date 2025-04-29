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
            self.shutdown_event.clear()
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

    def print_market_analysis(self, title: str, highest_price: float, optimal_price: float, current_price: float, min_price: float, max_price: float):
        table = Table(title=f"[bold]{self.instance_id} - Market Analysis for {title}[/bold]", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Current Price", f"${current_price:.2f}")
        table.add_row("Highest Competitor", f"${highest_price:.2f}")
        table.add_row("Optimal Price", f"${optimal_price:.2f}")
        table.add_row("Price Difference", f"${(optimal_price - current_price):.2f}")
        table.add_row("Minimum Allowed Price", f"${min_price:.2f}")
        table.add_row("Maximum Allowed Price", f"${max_price:.2f}")

        self.console.print(table)
        logger.info(f"[{self.instance_id}] Market analysis for {title} - Optimal Price: ${optimal_price:.2f}")

    def print_action_result(self, action: str, details: str):
        self.console.print(f"[bold blue]{self.instance_id} - {action}:[/bold blue] [green]{details}[/green]")
        logger.info(f"[{self.instance_id}] {action}: {details}")


    def update_target(self, title: str, current_price: float, current_target: Dict):
        """
        Updates a specific target by checking market prices, deleting the old target (if applicable),
        and creating a new one at the optimal price, respecting configured or default min/max limits.
        """
        max_retries = 5  # Maximum number of retries for API calls
        backoff_factor = 2  # Base for exponential backoff

        try:
            # --- 1. Log Initial Information & Extract Attributes ---
            self.print_target_info(
                title,
                current_price,
                {attr["Name"]: attr["Value"] for attr in current_target.get("Attributes", [])}
            )
            self.console.print(f"[cyan][{self.instance_id}] Starting update for '{title}' - Current Price: ${current_price:.2f}[/cyan]")
            logger.info(f"[{self.instance_id}] Starting update for '{title}' - Current Price: ${current_price:.2f}, TargetID: {current_target.get('TargetID', 'N/A')}")

            target_attrs = {a["Name"]: a["Value"] for a in current_target.get("Attributes", [])}
            phase = target_attrs.get("phase", "")
            float_val = target_attrs.get("floatPartValue", "")
            seed = target_attrs.get("paintSeed", "")

            # --- 2. Determine Min/Max Price Constraints ---
            max_price_config = self.bot_manager.get_max_price(title, phase, float_val, seed)
            min_price_config = self.bot_manager.get_min_price(title, phase, float_val, seed)

            max_price_to_use = max_price_config
            min_price_to_use = min_price_config

            if max_price_config == float('inf'):
                # No specific max price found in config, calculate and set a default
                calculated_default_max = round(current_price * 1.5, 2)
                default_min_price_for_new = 0.0 # Use 0.0 as the default minimum

                # Save this default rule for future runs
                added_default = self.bot_manager.ensure_price_entry_exists(
                    title, phase, float_val, seed, calculated_default_max, default_min_price_for_new
                )

                if added_default:
                    log_msg = f"[{self.instance_id}] No config found for '{title}' (Attrs: phase='{phase}', float='{float_val}', seed='{seed}'). Setting default max price: ${calculated_default_max:.2f}, default min price: ${default_min_price_for_new:.2f}"
                    self.console.print(f"[yellow]{log_msg}[/yellow]")
                    logger.info(log_msg)

                # Use the calculated defaults for *this* update cycle
                max_price_to_use = calculated_default_max
                min_price_to_use = default_min_price_for_new
            # else: Config found, max_price_to_use and min_price_to_use are already set correctly


            # --- 3. Delete Old Target (if not first cycle) ---
            target_id_to_delete = current_target.get("TargetID")
            deletion_successful = True # Assume success unless deletion is attempted and fails

            if self.first_cycle_complete and target_id_to_delete:
                self.console.print(f"[{self.instance_id}] [yellow]Attempting to delete existing target: {target_id_to_delete}[/yellow]")
                retries = 0
                deletion_successful = False # Reset flag as we are attempting deletion now
                while retries <= max_retries:
                    try:
                        self.api.delete_target(target_id_to_delete)
                        self.console.print(f"[{self.instance_id}] [green]Successfully deleted target: {target_id_to_delete}[/green]")
                        logger.info(f"[{self.instance_id}] Deleted target: {target_id_to_delete}")
                        deletion_successful = True
                        break # Exit retry loop on success
                    except Exception as e:
                        retries += 1
                        error_msg = f"Error deleting target {target_id_to_delete}: {e}"
                        logger.warning(f"[{self.instance_id}] {error_msg} (Attempt {retries}/{max_retries})")
                        if retries > max_retries:
                            self.console.print(f"[bold red][{self.instance_id}] Failed to delete target {target_id_to_delete} after {max_retries} retries: {e}[/bold red]", style="red")
                            logger.error(f"[{self.instance_id}] Failed to delete target {target_id_to_delete} after {max_retries} retries.", exc_info=True)
                            # If deletion fails critically, we might not want to proceed
                            # or handle it differently (e.g., skip creating a new one to avoid duplicates)
                            # For now, we'll let it continue but log the failure.
                            break # Exit loop after max retries
                        else:
                            backoff_time = (backoff_factor ** retries) + random.uniform(0, 1)
                            self.console.print(f"[bold yellow][{self.instance_id}] Retrying target deletion for {target_id_to_delete} in {backoff_time:.2f} seconds...[/bold yellow]", style="yellow")
                            time.sleep(backoff_time)
            elif not target_id_to_delete:
                 logger.warning(f"[{self.instance_id}] Cannot delete target for '{title}' as TargetID is missing in current_target data.")
                 deletion_successful = False # Cannot proceed reliably without deleting


            # --- 4. Fetch Market Prices ---
            self.console.print(f"[{self.instance_id}] [blue]Fetching market prices for '{title}'...[/blue]")
            try:
                market_prices = self.api.get_market_prices(title)
            except Exception as e:
                logger.error(f"[{self.instance_id}] Failed to fetch market prices for '{title}': {e}", exc_info=True)
                self.console.print(f"[bold red][{self.instance_id}] Error fetching market prices for {title}: {e}[/bold red]", style="red")
                # Decide recovery strategy: maybe recreate at old price if deletion happened?
                # For now, we log and exit the update for this item.
                return

            # --- 5. Analyze Market and Determine Optimal Price ---
            if not market_prices.get("orders"):
                self.console.print(f"[{self.instance_id}] [red]No market orders found for '{title}'.[/red]")
                logger.warning(f"[{self.instance_id}] No market orders found for '{title}'.")
                optimal_price = current_price # Fallback to current price if no market data
                highest_price = current_price # For analysis printout
                relevant_orders_found = False
            else:
                relevant_orders = []
                for order in market_prices["orders"]:
                    order_attributes = order.get("attributes", {})
                    attributes_match = True

                    # Check phase only if target has a specific phase
                    if phase and phase != order_attributes.get("phase"):
                        attributes_match = False
                    # Check float only if target has a specific float
                    if float_val and float_val != order_attributes.get("floatPartValue"):
                         attributes_match = False
                    # Check seed only if target has a specific seed
                    if seed and seed != order_attributes.get("paintSeed"):
                         attributes_match = False

                    if attributes_match:
                        relevant_orders.append(order)

                if not relevant_orders:
                    self.console.print(f"[{self.instance_id}] [red]No relevant orders found for '{title}' with matching attributes.[/red]")
                    logger.warning(f"[{self.instance_id}] No relevant orders found for '{title}' with matching attributes (Phase: '{phase}', Float: '{float_val}', Seed: '{seed}').")
                    optimal_price = current_price # Fallback to current price
                    highest_price = current_price # For analysis printout
                    relevant_orders_found = False
                else:
                    relevant_orders_found = True
                    # Calculate optimal price based on relevant orders
                    try:
                        highest_price = max(float(order["price"]) / 100 for order in relevant_orders)
                        optimal_price = round(highest_price + 0.01, 2)
                    except (ValueError, KeyError) as e:
                         logger.error(f"[{self.instance_id}] Error processing relevant order prices for '{title}': {e}. Orders: {relevant_orders}", exc_info=True)
                         self.console.print(f"[bold red][{self.instance_id}] Error processing market order data for '{title}'.[/bold red]", style="red")
                         optimal_price = current_price # Fallback
                         highest_price = current_price # Fallback

            # Apply min price constraint
            if optimal_price < min_price_to_use:
                self.console.print(f"[{self.instance_id}] [yellow]Optimal price ${optimal_price:.2f} is below minimum ${min_price_to_use:.2f}. Adjusting to minimum.[/yellow]")
                logger.info(f"[{self.instance_id}] Adjusting optimal price for '{title}' from ${optimal_price:.2f} to minimum ${min_price_to_use:.2f}.")
                optimal_price = min_price_to_use

            # Apply max price constraint (important check AFTER min adjustment)
            if optimal_price > max_price_to_use:
                self.console.print(f"[{self.instance_id}] [red]Optimal price ${optimal_price:.2f} exceeds max price ${max_price_to_use:.2f}. Adjusting to max price.[/red]")
                logger.info(f"[{self.instance_id}] Adjusting optimal price for '{title}' from ${optimal_price:.2f} to maximum ${max_price_to_use:.2f}.")
                optimal_price = max_price_to_use

            # Print market analysis using the determined constraints for this run
            self.print_market_analysis(title, highest_price, optimal_price, current_price, min_price_to_use, max_price_to_use)

            # --- 6. Create New Target (or Recreate Original if Necessary) ---
            should_create_new_target = False
            price_for_creation = optimal_price

            if not self.first_cycle_complete:
                self.console.print(f"[{self.instance_id}] [magenta]First cycle: Skipping price update logic. Will ensure target exists at original price.[/magenta]")
                logger.info(f"[{self.instance_id}] First cycle for '{title}'. Skipping optimal price update.")
                # If deletion didn't happen (because it's the first cycle), we don't strictly *need* to recreate.
                # However, to be safe and ensure the target is listed, we can force a recreation check/attempt.
                # For simplicity in this version, we'll rely on the next cycle to fully update if needed.
                # If the target *was* deleted somehow before the first cycle check (unlikely), this logic might need adjusting.
                # Let's assume the target still exists if deletion was skipped. No creation needed here.
                should_create_new_target = False # Explicitly don't create based on optimal price yet

            elif not deletion_successful and target_id_to_delete:
                 self.console.print(f"[bold red][{self.instance_id}] Skipping target creation for '{title}' because previous target deletion failed.[/bold red]", style="red")
                 logger.error(f"[{self.instance_id}] Skipped target creation for '{title}' due to failed deletion of {target_id_to_delete}.")
                 should_create_new_target = False

            elif current_price == optimal_price:
                self.console.print(f"[{self.instance_id}] [green]Price ${current_price:.2f} is already optimal. Recreating target.[/green]")
                logger.info(f"[{self.instance_id}] Price for '{title}' is already optimal (${current_price:.2f}). Recreating target.")
                should_create_new_target = True # Recreate even if optimal, because old one was deleted
                price_for_creation = current_price # Use the existing price

            else: # Price is different and deletion was successful (or skipped on first cycle)
                self.console.print(f"[{self.instance_id}] [green]Price changed. Creating new target at optimal price: ${optimal_price:.2f}[/green]")
                logger.info(f"[{self.instance_id}] Price for '{title}' changed from ${current_price:.2f} to ${optimal_price:.2f}. Creating new target.")
                should_create_new_target = True
                price_for_creation = optimal_price

            # Perform the creation if decided
            if should_create_new_target:
                self.print_action_result(
                    "Preparing to create target",
                    f"Item: '{title}', Price: ${price_for_creation:.2f}, Attributes: {current_target.get('Attributes', [])}"
                )

                # Add a small delay before creating, especially after deletion
                creation_delay = random.uniform(1.0, 2.5) # Random delay between 1 and 2.5 seconds
                self.console.print(f"[{self.instance_id}] [yellow]Waiting {creation_delay:.2f}s before creating target...[/yellow]")
                time.sleep(creation_delay)

                retries = 0
                creation_successful = False
                while retries <= max_retries:
                    try:
                        response = self.api.create_target(
                            title=title,
                            amount=current_target.get("Amount", "1"), # Default to 1 if amount missing
                            price=price_for_creation,
                            attributes=current_target.get("Attributes", [])
                        )
                        # Optional: Check response content if needed
                        log_msg = f"Successfully created target for '{title}' at ${price_for_creation:.2f}. Response: {response}"
                        self.console.print(f"[{self.instance_id}] [green]Successfully created target.[/green]")
                        logger.info(f"[{self.instance_id}] {log_msg}")
                        creation_successful = True
                        break # Exit retry loop on success
                    except Exception as e:
                        retries += 1
                        error_msg = f"Error creating target for '{title}' at ${price_for_creation:.2f}: {e}"
                        logger.warning(f"[{self.instance_id}] {error_msg} (Attempt {retries}/{max_retries})")
                        if retries > max_retries:
                            self.console.print(f"[bold red][{self.instance_id}] Failed to create target for '{title}' after {max_retries} retries: {e}[/bold red]", style="red")
                            logger.error(f"[{self.instance_id}] Failed to create target for '{title}' after {max_retries} retries.", exc_info=True)
                            break # Exit loop after max retries
                        else:
                            backoff_time = (backoff_factor ** retries) + random.uniform(0, 1)
                            self.console.print(f"[bold yellow][{self.instance_id}] Retrying target creation for '{title}' in {backoff_time:.2f} seconds...[/bold yellow]", style="yellow")
                            time.sleep(backoff_time)


        except Exception as e:
            # Catch-all for any unexpected errors during the process
            logger.error(f"[{self.instance_id}] Unexpected error in update_target for '{title}': {str(e)}", exc_info=True)
            self.console.print(f"[bold red][{self.instance_id}] Unexpected error updating target '{title}':[/bold red] {str(e)}", style="red")

        finally:
            # Optional: Add any cleanup or final logging here if needed
            logger.debug(f"[{self.instance_id}] Finished update cycle for '{title}'.")

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

    def update_max_price(self, item_name: str, phase: str, float_val: str, seed: str, max_price: float, min_price: float):
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
            'max_price': max_price,
            'min_price': min_price
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
        best_entry = max(matching, key=lambda x: sum(1 for k in ['phase', 'float', 'seed'] if x.get(k, '')))
        return best_entry.get('max_price', float('inf'))

    def ensure_price_entry_exists(self, item_name: str, phase: str, float_val: str, seed: str, default_max_price: float, default_min_price: float = 0.0):
        """
        Checks if an exact price entry exists. If not, adds a new one with default values.
        Returns True if a new entry was added, False otherwise.
        """
        # Check if an EXACT entry already exists
        for entry in self.max_prices:
            if (entry['item'] == item_name and
                entry.get('phase', '') == phase and
                entry.get('float', '') == float_val and
                entry.get('seed', '') == seed):
                return False # Exact entry already exists, do nothing

        # No exact entry found, add the new default entry
        self.max_prices.append({
            'item': item_name,
            'phase': phase,
            'float': float_val,
            'seed': seed,
            'max_price': default_max_price,
            'min_price': default_min_price  # Use the provided default min price
        })
        self.save_max_prices()
        logger.info(f"Added default price entry for '{item_name}' ({phase}, {float_val}, {seed}): Max=${default_max_price:.2f}, Min=${default_min_price:.2f}")
        return True

    # Modify get_max_price and get_min_price slightly for clarity if needed,
    # but their core logic of finding the best match remains the same.
    # The default float('inf') for max and 0.0 for min are still correct signals
    # for when no rule is found.

    def get_max_price(self, item_name: str, phase: str, float_val: str, seed: str) -> float:
        matching = []
        for entry in self.max_prices:
            if entry['item'] != item_name:
                continue
            match = True
            # Only consider entries where attributes match IF the entry specifies that attribute
            if entry.get('phase', '') and entry['phase'] != phase:
                match = False
            if entry.get('float', '') and entry['float'] != float_val:
                match = False
            if entry.get('seed', '') and entry['seed'] != seed:
                match = False
            if match:
                matching.append(entry)

        if not matching:
            # CRITICAL: Return float('inf') to signal that no configuration was found.
            return float('inf')

        # Find the most specific matching rule
        best_entry = max(matching, key=lambda x: sum(1 for k in ['phase', 'float', 'seed'] if x.get(k, '')))
        return best_entry.get('max_price', float('inf')) # Default to 'inf' if key missing, though unlikely

    def get_min_price(self, item_name: str, phase: str, float_val: str, seed: str) -> float:
        matching = []
        for entry in self.max_prices:
            if entry['item'] != item_name:
                continue
            match = True
            # Only consider entries where attributes match IF the entry specifies that attribute
            if entry.get('phase', '') and entry['phase'] != phase:
                match = False
            if entry.get('float', '') and entry['float'] != float_val:
                match = False
            if entry.get('seed', '') and entry['seed'] != seed:
                match = False
            if match:
                matching.append(entry)

        if not matching:
             # CRITICAL: Return 0.0 to signal that no configuration was found (a safe default min)
            return 0.0

        # Find the most specific matching rule
        best_entry = max(matching, key=lambda x: sum(1 for k in ['phase', 'float', 'seed'] if x.get(k, '')))
        return best_entry.get('min_price', 0.0) # Default to 0.0 if key missing

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