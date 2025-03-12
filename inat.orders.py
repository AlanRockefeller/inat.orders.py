# inat.orders.py 
# 
# A python script that summarizes the orders (taxonomic rank) of iNaturalist observations - and optionally adds a summary of the families
# Useful for generating the data needed for a transportation permit to get scientific collections into a herbarium
# Also useful for generating summary data for scientific papers

# By Alan Rockefeller - March 12, 2025

# For more information see https://github.com/AlanRockefeller/inat.orders.py

import sys
import requests
import time
import argparse
from collections import Counter, defaultdict

# The iNaturalist API doesn't like it when there is more than one request per second
class RateLimiter:
    """
    Manages API request timing to respect rate limits.
    """
    def __init__(self, min_delay=1.0, debug=False):
        self.min_delay = min_delay
        self.last_call_time = 0
        self.call_count = 0
        self.debug = debug

    def wait_and_increment(self):
        """
        Waits if necessary to respect the rate limit, then marks a new API call.
        """
        now = time.time()
        elapsed = now - self.last_call_time

        # If this isn't the first call and we haven't waited long enough
        if self.last_call_time > 0 and elapsed < self.min_delay:
            wait_time = self.min_delay - elapsed
            if self.debug:
                print(f"Rate limiting: waiting {wait_time:.2f} seconds...", file=sys.stderr)
            time.sleep(wait_time)

        # Update the last call time after waiting (if needed)
        self.last_call_time = time.time()
        self.call_count += 1

    def get_count(self):
        """
        Returns the total number of API calls made.
        """
        return self.call_count

# Global rate limiter instance (debug will be set in main)
rate_limiter = RateLimiter()

def make_api_request(url, min_delay=1.0, retries=3, retry_delay=2.0):
    """
    Makes an API request with rate limiting and retry logic.
    """
    rate_limiter.min_delay = min_delay  # Update the rate limiter's delay setting

    for attempt in range(retries):
        # Wait as needed to respect rate limits
        rate_limiter.wait_and_increment()

        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 429:  # Too Many Requests
                if attempt < retries - 1:  # If we have more retries left
                    if rate_limiter.debug:
                        print(f"Rate limit exceeded. Waiting {retry_delay * (attempt + 1)} seconds...", file=sys.stderr)
                    time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                    continue
            # If it's not a rate limit or we're out of retries, re-raise
            raise e
        except Exception as e:
            # For any other exception, re-raise
            raise e

def get_taxon_info(taxon_id, min_delay=1.0):
    """
    Fetches information about a specific taxon ID from the iNaturalist API
    """
    url = f"https://api.inaturalist.org/v1/taxa/{taxon_id}"
    return make_api_request(url, min_delay)

def get_observation_taxonomy(observation_id, min_delay=1.0, include_family=False):
    """
    Fetches the taxonomic information for a given iNaturalist observation ID.
    Returns tuple of (order_name, family_name, error_message, current_rank, current_rank_name).
    If include_family is False, family_name will be None.
    """
    url = f"https://api.inaturalist.org/v1/observations/{observation_id}"
    try:
        data = make_api_request(url, min_delay)

        if not data.get('results') or len(data['results']) == 0:
            return (None, None, "No results found", None, None)

        taxon = data['results'][0].get('taxon')
        if not taxon:
            return (None, None, "No taxonomic information available", None, None)

        # Record the current taxon's rank and name
        current_rank = taxon.get('rank')
        current_rank_name = taxon.get('name')

        # Initialize return values
        order_name = None
        family_name = None

        # Check if the current taxon is already at order or family rank
        if current_rank == 'order':
            order_name = current_rank_name
        elif current_rank == 'family':
            family_name = current_rank_name

        # Get the ancestry chain
        ancestry = taxon.get('ancestry')
        if not ancestry:
            return (order_name, family_name, "No ancestry information available", current_rank, current_rank_name)

        # Split the ancestry into IDs
        ancestor_ids = ancestry.split('/')

        # If the current taxon is not an order or family, we need to check ancestors
        if not order_name or (include_family and not family_name):
            for ancestor_id in ancestor_ids:
                try:
                    ancestor_info = get_taxon_info(ancestor_id, min_delay)
                    if (ancestor_info.get('results') and len(ancestor_info['results']) > 0):
                        ancestor_rank = ancestor_info['results'][0].get('rank')
                        if ancestor_rank == 'order' and not order_name:
                            order_name = ancestor_info['results'][0].get('name')
                        elif include_family and ancestor_rank == 'family' and not family_name:
                            family_name = ancestor_info['results'][0].get('name')

                        # If we have found both required taxonomic ranks, we can stop searching
                        if order_name and (not include_family or family_name):
                            break
                except Exception as e:
                    if rate_limiter.debug:
                        print(f"Warning: Failed to get ancestor info for {ancestor_id}: {str(e)}", file=sys.stderr)
                    # Continue searching other ancestors rather than failing completely

        # Return the results
        if not order_name:
            return (None, family_name, "Could not find order in ancestry chain", current_rank, current_rank_name)
        return (order_name, family_name, None, current_rank, current_rank_name)

    except requests.exceptions.RequestException as e:
        return (None, None, f"API request failed: {str(e)}", None, None)
    except Exception as e:
        return (None, None, f"Error processing observation: {str(e)}", None, None)

def main():
    parser = argparse.ArgumentParser(description='Look up taxonomic information for iNaturalist observations.')
    parser.add_argument('observation_ids', nargs='+', help='One or more iNaturalist observation IDs')
    parser.add_argument('--count-api-calls', action='store_true',
                        help='Print the total number of API calls made')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Minimum delay in seconds between API calls (default: 1.0)')
    parser.add_argument('--family', action='store_true',
                        help='Include family taxonomic rank in the output')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug output for rate limiting and API calls')

    args = parser.parse_args()

    # Set the rate limiter's delay and debug settings
    rate_limiter.min_delay = args.delay
    rate_limiter.debug = args.debug

    # Counters for summarizing
    order_counter = Counter()
    unknown_order_count = 0
    
    # Organize families by their orders - for the --family summary
    order_family_map = defaultdict(Counter)
    unknown_family_by_order = defaultdict(int)
    unknown_family_unknown_order_count = 0

    for obs_id in args.observation_ids:
        try:
            order_name, family_name, error, current_rank, current_rank_name = get_observation_taxonomy(
                obs_id, args.delay, args.family
            )

            if error:
                if current_rank and current_rank_name:
                    # Format the rank with first letter capitalized
                    formatted_rank = current_rank.capitalize()
                    print(f"{obs_id}: {formatted_rank}: {current_rank_name}")
                    # Count as unknown for summary
                    unknown_order_count += 1
                    if args.family:
                        unknown_family_unknown_order_count += 1
                else:
                    print(f"{obs_id}: Error - {error}")
                    # Count errors as unknown
                    unknown_order_count += 1
                    if args.family:
                        unknown_family_unknown_order_count += 1
            else:
                if args.family:
                    if family_name:
                        print(f"{obs_id}: Order: {order_name} Family: {family_name}")
                        # Track families by order
                        order_family_map[order_name][family_name] += 1
                    else:
                        print(f"{obs_id}: Order: {order_name} Family: Unknown")
                        # Track unknown families by order
                        unknown_family_by_order[order_name] += 1
                else:
                    print(f"{obs_id}: {order_name}")

                # Add to order counter for summary
                order_counter[order_name] += 1
        except Exception as e:
            print(f"{obs_id}: Error - Unexpected error: {str(e)}")
            # Count exceptions as unknown
            unknown_order_count += 1
            if args.family:
                unknown_family_unknown_order_count += 1

    # Print API call count if requested
    if args.count_api_calls:
        print(f"\nTotal API calls made: {rate_limiter.get_count()}")

    # Print summary if more than one observation was processed
    if len(args.observation_ids) > 1:
        print("\nSummary by Order:")
        # Sort by count (most to least)
        for order, count in sorted(order_counter.items(), key=lambda x: x[1], reverse=True):
            print(f"{count:6d}  {order}")

        # Add unknown order count if any
        if unknown_order_count > 0:
            print(f"{unknown_order_count:6d}  Unknown order")

        # Add family summary if requested
        if args.family:
            # For each order, print its family summary
            for order in sorted(order_counter.keys()):
                print(f"\nFamilies within {order}:")
                # Sort families within this order by count
                for family, count in sorted(order_family_map[order].items(), key=lambda x: x[1], reverse=True):
                    print(f"{count:6d}  {family}")
                
                # Add unknown family count for this order if any
                if unknown_family_by_order[order] > 0:
                    print(f"{unknown_family_by_order[order]:6d}  Unknown family")
            
            # Print summary for observations with unknown orders but known families (unlikely but possible)
            if unknown_family_unknown_order_count > 0:
                print(f"\nUnknown families within unknown orders: {unknown_family_unknown_order_count}")

if __name__ == "__main__":
    main()
