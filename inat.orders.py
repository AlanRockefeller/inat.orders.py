import sys
import requests
import time
import argparse
from collections import Counter, defaultdict

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

def make_api_request(url, min_delay=1.0, retries=5, retry_delay=5.0, max_backoff=120.0):
    """
    Makes an API request with rate limiting and retry logic.
    
    Parameters:
    - url: The API URL to request
    - min_delay: Minimum delay between requests in seconds
    - retries: Maximum number of retry attempts
    - retry_delay: Initial delay for retry in seconds
    - max_backoff: Maximum backoff time in seconds
    """
    rate_limiter.min_delay = min_delay  # Update the rate limiter's delay setting

    for attempt in range(retries):
        # Wait as needed to respect rate limits
        rate_limiter.wait_and_increment()

        try:
            headers = {
                'User-Agent': 'Taxonomy-Extractor/1.0 (Research Project; Contact: your-email@example.com)'
            }
            response = requests.get(url, headers=headers)
            
            # Check for rate limit information in headers
            if 'X-RateLimit-Remaining' in response.headers and rate_limiter.debug:
                remaining = response.headers.get('X-RateLimit-Remaining')
                reset_time = response.headers.get('X-RateLimit-Reset')
                print(f"Rate limit info: {remaining} requests remaining. Reset at: {reset_time}", file=sys.stderr)
            
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 429:  # Too Many Requests
                if attempt < retries - 1:  # If we have more retries left
                    # Calculate backoff time with exponential increase but cap at max_backoff
                    backoff_time = min(retry_delay * (2 ** attempt), max_backoff)
                    
                    # Use Retry-After header if available
                    retry_after = response.headers.get('Retry-After')
                    if retry_after:
                        try:
                            backoff_time = max(float(retry_after), backoff_time)
                        except (ValueError, TypeError):
                            pass  # Use calculated backoff if Retry-After is not a valid number
                    
                    if rate_limiter.debug:
                        print(f"Rate limit exceeded. Attempt {attempt+1}/{retries}. Waiting {backoff_time:.1f} seconds...", 
                              file=sys.stderr)
                    time.sleep(backoff_time)
                    
                    # Increase the minimum delay for future requests
                    rate_limiter.min_delay = min(rate_limiter.min_delay * 1.5, 10.0)
                    continue
                else:
                    print(f"ERROR: Maximum retries reached for URL: {url}", file=sys.stderr)
            # If it's not a rate limit or we're out of retries, re-raise
            raise e
        except requests.exceptions.ConnectionError as e:
            # Handle connection errors with backoff
            if attempt < retries - 1:
                backoff_time = min(retry_delay * (2 ** attempt), max_backoff)
                if rate_limiter.debug:
                    print(f"Connection error. Attempt {attempt+1}/{retries}. Waiting {backoff_time:.1f} seconds...", 
                          file=sys.stderr)
                time.sleep(backoff_time)
                continue
            raise e
        except Exception as e:
            # For any other exception, re-raise
            raise e

def get_taxon_info(taxon_id, min_delay=1.0):
    """
    Fetches information about a specific taxon ID from iNaturalist.
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
    parser.add_argument('observation_ids', nargs='*', help='One or more iNaturalist observation IDs')
    parser.add_argument('--file', type=str, help='File containing observation IDs, one per line')
    parser.add_argument('--users', action='store_true', 
                        help='Input IDs are usernames, not observation IDs')
    parser.add_argument('--count-api-calls', action='store_true',
                        help='Print the total number of API calls made')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Minimum delay in seconds between API calls (default: 1.0)')
    parser.add_argument('--max-delay', type=float, default=10.0,
                        help='Maximum delay between API calls when rate limited (default: 10.0)')
    parser.add_argument('--retry-delay', type=float, default=5.0,
                        help='Initial retry delay in seconds (default: 5.0)')
    parser.add_argument('--retries', type=int, default=5,
                        help='Maximum number of retry attempts (default: 5)')
    parser.add_argument('--batch-size', type=int, default=1000,
                        help='Process observations in batches of this size (default: 1000)')
    parser.add_argument('--batch-pause', type=float, default=60.0,
                        help='Pause in seconds between batches (default: 60.0)')
    parser.add_argument('--family', action='store_true',
                        help='Include family taxonomic rank in the output')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug output for rate limiting and API calls')
    parser.add_argument('--resume-from', type=str, default=None,
                        help='Resume from a specific observation ID')

    args = parser.parse_args()

    # Load observation IDs from file if specified
    observation_ids = args.observation_ids
    if args.file:
        try:
            with open(args.file, 'r') as f:
                file_ids = [line.strip() for line in f if line.strip()]
                observation_ids.extend(file_ids)
        except Exception as e:
            print(f"Error reading file {args.file}: {str(e)}")
            sys.exit(1)
    
    if not observation_ids:
        print("No observation IDs provided. Use positional arguments or --file option.")
        sys.exit(1)

    # Set the rate limiter's delay and debug settings
    rate_limiter.min_delay = args.delay
    rate_limiter.debug = args.debug

    # Counters for summarizing
    order_counter = Counter()
    unknown_order_count = 0
    
    # Organize families by their orders
    order_family_map = defaultdict(Counter)
    unknown_family_by_order = defaultdict(int)
    unknown_family_unknown_order_count = 0
    
    # For resuming functionality
    start_index = 0
    
    # If resuming from a specific ID
    if args.resume_from:
        try:
            start_index = observation_ids.index(args.resume_from)
            print(f"Resuming from observation ID {args.resume_from} (index {start_index})")
        except ValueError:
            print(f"Warning: Resume ID {args.resume_from} not found in the list. Starting from the beginning.")
    
    # Process observations in batches
    total_observations = len(observation_ids[start_index:])
    batch_size = args.batch_size
    observation_batches = [observation_ids[start_index:][i:i+batch_size] 
                          for i in range(0, total_observations, batch_size)]
    
    print(f"Processing {total_observations} observations in {len(observation_batches)} batches of size {batch_size}")
    
    # Track failed observations for potential retry
    failed_observations = []
    
    # Process each batch
    for batch_num, batch_obs_ids in enumerate(observation_batches):
        print(f"\nProcessing batch {batch_num+1}/{len(observation_batches)} " +
              f"({len(batch_obs_ids)} observations)...")
        
        if batch_num > 0 and args.batch_pause > 0:
            print(f"Pausing for {args.batch_pause} seconds between batches...")
            time.sleep(args.batch_pause)
        
        # Process each observation in the batch
        for obs_id in batch_obs_ids:
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
                
                # Add to failed observations list
                failed_observations.append(obs_id)

    # Report on failed observations and offer to save them
    if failed_observations:
        print(f"\n{len(failed_observations)} observations failed to process.")
        
        # Save failed IDs to a file for later retry
        failed_file = f"failed_observations_{int(time.time())}.txt"
        with open(failed_file, 'w') as f:
            for failed_id in failed_observations:
                f.write(f"{failed_id}\n")
        print(f"Failed observation IDs saved to {failed_file}")
        print(f"To retry these, use: python taxa_lookup.py $(cat {failed_file}) --family")
    
    # Print API call count if requested
    if args.count_api_calls:
        print(f"\nTotal API calls made: {rate_limiter.get_count()}")

    # Print summary if more than one observation was processed
    if len(observation_ids) > 1:
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
