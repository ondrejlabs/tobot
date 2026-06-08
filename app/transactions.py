# Standard library imports
import json
import locale
from pathlib import Path

# Third-party imports
from loguru import logger
import pandas as pd

# Local imports
from config import CONFIG
import app.exchange as exchange

#Use the default regional settings (locale) of the user's operating system
locale.setlocale(locale.LC_ALL, '')

def run_transaction_job(folder_path: str | Path) -> str:
    """Main function to process transactions from JSON files in the specified folder, perform calculations, and save results."""

    folder_path = Path(folder_path)
    df = load_transactions_from_folder(folder_path)

    print(f"Number of transactions: {len(df)}:")
    print(df[['date', 'ticker', 'isin', 'security_name', 'quantity', 'value', 'currency']])

    df_map = load_mapping_data()
    print("Mapping Data:")
    print(df_map)

    # Apply the function across all rows (axis=1) of the dataframe
    #df = df.apply(fill_missing_identifiers, mapping_df=df_map, axis=1, force_currency_sync=True) #TODO pick from config/UI
    # Pass the entire dataframe to the vectorized function at once
    df = fill_missing_identifiers_vectorized(df, df_map, force_currency_sync=True)

    def get_rate(row) -> float | None:
        """A helper function to handle the logic"""
        curr = row['currency']
        # Catch Pandas NaN or inherently invalid currency codes before hitting the API
        if pd.isna(curr) or not isinstance(curr, str) or len(curr.strip()) != 3:
            return None
        return exchange.get_fx_rate(row['date'], curr)

    # Apply it to the whole dataframe at once
    df['fx_rate'] = df.apply(get_rate, axis=1)

    # Identify rows where the exchange rate failed (either due to bad input or API failure)
    invalid_rates_mask = pd.isna(df['fx_rate'])

    if invalid_rates_mask.sum() > 0:
        bad_rows = df[invalid_rates_mask]
        assert isinstance(bad_rows, pd.DataFrame)

        for index, row in bad_rows.iterrows():
            logger.warning(
                f"Skipping row {index} due to missing/invalid exchange rate. "
                f"Currency evaluated: '{row['currency']}'. Row data: {row.to_dict()}"
            )
        logger.warning(f"Flagged {invalid_rates_mask.sum()} rows with invalid exchange rates. They will appear as FAILED in the output.")

    # Create the new 'tax' column using vectorized arithmetic
    # (If fx_rate is NaN, tax will automatically become NaN safely)
    df['tax'] = df['value'] / df['fx_rate'] * df['tax_rate']
    assert isinstance(df, pd.DataFrame)

    # Sort Intermediate CSV
    # Ensure 'filename' exists and has no NaNs to avoid sorting errors
    if 'filename' not in df.columns:
        df['filename'] = 'unknown'
    else:
        df['filename'] = df['filename'].fillna('unknown')

    # Sort by filename (a-z), then by original position
    df = df.sort_values(by=['filename', 'position'], ascending=[True, True])

    # Determine the target filename postfix based on the dates
    target_period = get_target_period(df)

    csv_filename = f'transactions_{target_period}.csv'
    csv_filepath = folder_path / csv_filename
    df.to_csv(csv_filepath, index=False)

    logger.info(f"Saved list of transactions to '{csv_filename}'")
    print("List of mapped transactions:")
    print(df)

    # Sort the DataFrame before summarizing to ensure consistent output order
    df_sorted = df.sort_values(by=['tax_rate', 'date', 'ticker'])

    summary = summarize_taxes(df_sorted)
    summary_output = []

    # Loop through the results and add them to the output
    for tax_rate, data in summary.items():
        summary_output.append(f"--- Tax Rate: {tax_rate:.2%} ---")

        total_tax_fmt = locale.format_string("%.2f", data['total_tax'], grouping=True)
        summary_output.append(f"Total Tax: {total_tax_fmt}")

        summary_output.append(f"Number of Operations: {data['operation_count']}")

        total_val_fmt = locale.format_string("%.2f", data['total_converted_value'], grouping=True)
        summary_output.append(f"Total Value (EUR): {total_val_fmt}")

        total_fee_fmt = locale.format_string("%.2f", data['total_converted_fees'], grouping=True)
        summary_output.append(f"Total Fees (EUR): {total_fee_fmt}")

        summary_output.append("Transactions:")
        transactions_str = (
            data['transactions'][['date', 'ticker', 'value', 'fee', 'fx_rate', 'tax']]
            .assign(
                value=lambda x: x['value'].map(lambda v: locale.format_string("%.2f", v, grouping=True)),
                fee=lambda x: x['fee'].map(lambda f: locale.format_string("%.2f", f, grouping=True)),
                
                # Intercept NaN values and print a highly visible "FAILED" string
                fx_rate=lambda x: x['fx_rate'].map(
                    lambda fx: "FAILED" if pd.isna(fx) else locale.format_string("%.4f", fx, grouping=True)
                ),
                tax=lambda x: x['tax'].map(
                    lambda t: "FAILED" if pd.isna(t) else locale.format_string("%.2f", t, grouping=True)
                )
            )
            .to_string(index=False)
        )
        summary_output.append(transactions_str)

    # Save the summary to a text file in the same folder
    summary_filename = f'tob_summary_{target_period}.txt'
    summary_filepath = folder_path / summary_filename
    summary_filepath.write_text("\n".join(summary_output), encoding='utf-8')

    # Notify the user about the saved summary and print it to the console
    logger.info(f"Saved TOB summary to '{summary_filename}'")
    #print("\n".join(summary_output))

    return "\n".join(summary_output)

def load_transactions_from_folder(folder_path: Path) -> pd.DataFrame:
    """ Reads all .json files in the given folder and combines them into a single Pandas DataFrame. """
    all_transactions = []

    # Create a Path object for the directory
    target_dir = Path(folder_path)

    print(f"Number of JSON files in '{target_dir}': {len(list(target_dir.glob('*.json')))}")

    # Iterate through every .json file in the folder
    for file_path in target_dir.glob("*.json"):
        with open(file_path, "r", encoding="utf-8") as file:
            try:
                file_data = json.load(file)

                if isinstance(file_data, dict) and 'trades' in file_data:
                    broker = file_data.get('broker')
                    filename = file_data.get('filename')

                    for trade in file_data['trades']:
                        trade['broker'] = broker
                        trade['filename'] = filename

                    all_transactions.extend(file_data['trades'])

                elif isinstance(file_data, list):
                    all_transactions.extend(file_data)

            except json.JSONDecodeError:
                logger.warning(f"Could not decode {file_path.name}. Skipping.")

    # Create a single DataFrame from the combined list of dictionaries
    df = pd.DataFrame(all_transactions)

    # Clean up standard data types if the DataFrame isn't empty
    if not df.empty:

        # Handle Invalid Dates
        if 'date' in df.columns:
            raw_dates = df['date'].copy()
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            invalid_date_mask = df['date'].isna()

            if invalid_date_mask.sum() > 0:
                bad_rows = df[invalid_date_mask].copy()
                bad_rows['original_date_value'] = raw_dates[invalid_date_mask]

                for index, row in bad_rows.iterrows():
                    logger.debug(
                        f"Skipping row {index} due to invalid date. "
                        f"Found: '{row['original_date_value']}'. Row data: {row.to_dict()}"
                    )
                df = df.dropna(subset=['date']).reset_index(drop=True)
                logger.debug(f"Dropped {invalid_date_mask.sum()} invalid date rows.")

        # Drop rows where quantity or value is exactly 0
        if 'quantity' in df.columns and 'value' in df.columns:
            # Coerce to numeric just in case they came in as strings
            df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce')
            df['value'] = pd.to_numeric(df['value'], errors='coerce')

            zero_mask = (df['quantity'] == 0) | (df['value'] == 0)

            if zero_mask.sum() > 0:
                zero_rows = df[zero_mask]
                for index, row in zero_rows.iterrows():
                    logger.debug(
                        f"Skipping row {index} due to zero quantity/value. "
                        f"Qty: {row['quantity']}, Value: {row['value']}. Row data: {row.to_dict()}"
                    )
                # Keep only rows that are NOT in the zero_mask
                df = df[~zero_mask].reset_index(drop=True)
                logger.debug(f"Dropped {zero_mask.sum()} rows with zero quantity or value.")

        # Convert negative values to absolute values
        if 'value' in df.columns:
            neg_mask = df['value'] < 0

            if neg_mask.sum() > 0:
                neg_rows = df[neg_mask]
                assert isinstance(neg_rows, pd.DataFrame)
                for index, row in neg_rows.iterrows():
                    logger.debug(
                        f"Negative value detected in row {index}. "
                        f"Converting {row['value']} to absolute value. Row data: {row.to_dict()}"
                    )
                # Apply absolute value conversion
                df.loc[neg_mask, 'value'] = df.loc[neg_mask, 'value'].abs()
                logger.debug(f"Converted {neg_mask.sum()} negative values to absolute values.")

        # Drop rows missing all identifying information (Ticker, ISIN, Name)
        # Ensure the columns exist first to avoid KeyErrors
        if all(col in df.columns for col in ['ticker', 'isin', 'security_name']):

            # The '&' operator ensures the row is only flagged if ALL three are NaN
            missing_info_mask = pd.isna(df['ticker']) & pd.isna(df['isin']) & pd.isna(df['security_name'])

            if missing_info_mask.sum() > 0:
                missing_rows = df[missing_info_mask]
                assert isinstance(missing_rows, pd.DataFrame)
                for index, row in missing_rows.iterrows():
                    logger.debug(
                        f"Skipping row {index} due to missing ticker, ISIN, and security name (likely irrellevant - e.g. FX - operation). "
                        f"Row data: {row.to_dict()}"
                    )

                # Keep only rows that are NOT in the missing_info_mask
                valid_rows = df[~missing_info_mask]
                assert isinstance(valid_rows, pd.DataFrame)
                df = valid_rows.reset_index(drop=True)
                logger.debug(f"Dropped {missing_info_mask.sum()} rows missing all identifying information.")
    assert isinstance(df, pd.DataFrame)
    return df

def fill_missing_identifiers_vectorized(df: pd.DataFrame, df_map: pd.DataFrame, force_currency_sync=True) -> pd.DataFrame:
    """Vectorized filling of missing identifiers using cascading left joins."""

    cols = ['isin', 'ticker', 'security_name', 'currency', 'tax_rate']

    # 1. Deduplicate mapping rules to prevent row explosion during merges
    map_isin = df_map.drop_duplicates(subset=['isin'])
    map_tc = df_map.drop_duplicates(subset=['ticker', 'currency'])
    map_t = df_map.drop_duplicates(subset=['ticker'])

    # Save original index to align the merges perfectly
    df['orig_idx'] = df.index

    # 2. Attempt matches across the three tiers (how='left' ensures we don't drop unmatched rows)
    m1 = df[['orig_idx', 'isin']].merge(map_isin[cols], on='isin', how='left').set_index('orig_idx')
    m2 = df[['orig_idx', 'ticker', 'currency']].merge(map_tc[cols], on=['ticker', 'currency'], how='left').set_index('orig_idx')
    m3 = df[['orig_idx', 'ticker']].merge(map_t[cols], on='ticker', how='left').set_index('orig_idx')

    # 3. Combine the matches according to priority (ISIN > Ticker+Currency > Ticker)
    # combine_first() fills NaNs in the calling dataframe with values from the passed dataframe
    final_updates = m1[cols].combine_first(m2[cols]) # type: ignore

    if force_currency_sync:
        final_updates = final_updates.combine_first(m3[cols]) # type: ignore

    # 4. Overwrite the original dataframe with the successfully mapped values
    # df.update() modifies df in place, only replacing values where final_updates is NOT NaN
    df.update(final_updates)

    # Cleanup and return
    df.drop(columns=['orig_idx'], inplace=True)
    return df

def summarize_taxes(df) -> dict:
    """Summarizes tax information by tax rate, calculating total tax, total converted value, total converted fees, and operation count for each group."""
    results = {}

    for tax_rate, group in df.groupby('tax_rate'):
        # Safely calculate (value / fx_rate) per row, then sum it for the group
        total_converted_value = (group['value'] / group['fx_rate']).sum()
        total_converted_fees = (group['fee'] / group['fx_rate']).sum()

        results[tax_rate] = {
            'transactions': group,
            'total_tax': group['tax'].sum(),
            'operation_count': len(group),

            'total_converted_value': total_converted_value,
            'total_converted_fees': total_converted_fees
        }
    return results

def load_mapping_data() -> pd.DataFrame:
    """Loads the mapping CSV file into a Pandas DataFrame."""
    try:
        # We know CONFIG['mapping_csv_path'] is a valid, absolute pathlib.Path
        # because config.py validated it at startup.
        df_map = pd.read_csv(CONFIG['mapping_csv_path'])
        return df_map

    except FileNotFoundError:
        # This handles the edge case where the file was deleted AFTER startup
        logger.error("The mapping CSV file was moved or deleted while the app was running!")
        return pd.DataFrame() # Return empty dataframe to prevent crash

    except PermissionError:
        # This handles cases where the file is locked
        logger.error("Cannot read the CSV file. Please close it in Excel and try again.")
        return pd.DataFrame()

def get_target_period(df: pd.DataFrame) -> str:
    """
    Extracts the most frequent YYYY-MM period from the dataframe.
    Logs a warning if any dates fall outside of this period.
    """
    if 'date' not in df.columns or df.empty:
        logger.warning("Dataframe is empty or missing 'date' column. Using default filename.")
        return "UNKNOWN_DATE"

    # Convert dates to YYYY-MM strings
    periods = df['date'].dt.strftime('%Y-%m')

    # Find the most common YYYY-MM
    most_frequent_period = periods.mode()[0]

    # Identify any periods that don't match the most frequent one
    outliers = periods[periods != most_frequent_period]

    if not outliers.empty:
        unique_outliers = outliers.unique().tolist()
        logger.warning(
            f"Date outliers detected! Most frequent period is {most_frequent_period}, "
            f"but found transactions in these periods as well: {', '.join(unique_outliers)}."
        )
    return most_frequent_period
