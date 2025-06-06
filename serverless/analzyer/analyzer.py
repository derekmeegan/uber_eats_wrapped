import io
import json
import logging
import os
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List

import boto3
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from botocore.exceptions import ClientError
from sendgrid import SendGridAPIClient, Mail, To, Content
from sendgrid.helpers.mail import Email

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3_client = boto3.client('s3')

def _hour_label(hour: int) -> str:
    """Return `22 → '10PM'`, `9 → '9AM'`, etc."""
    return datetime(1900, 1, 1, hour).strftime("%-I%p")  # remove leading 0

def add_years_to_orders(
    orders: List[Dict[str, Any]],
    current_year: int = 2025,
) -> List[Dict[str, Any]]:
    """
    Insert years into the `date` field of each order.

    Assumptions
    -----------
    1. `orders` is already in reverse‑chronological order (most‑recent first).
    2. If the first order’s month is Jan–Jun (inclusive) we treat it as
       `current_year`; otherwise it belongs to `current_year - 1`.
    3. While scanning down the list, crossing from January back to any later
       month means we just stepped into the previous calendar year, so we
       decrement the running year each time that wrap happens.

    Returns the **same list**, mutated in place, for convenience.
    """
    if not orders:
        return orders

    month_to_num: Dict[str, int] = {
        m: i
        for i, m in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
            start=1,
        )
    }
    first_month: str = orders[0]["date"].split()[0].title()
    year: int = current_year if first_month in {
        "Jan", "Feb", "Mar", "Apr", "May", "Jun"
    } else current_year - 1

    prev_month_num: int = month_to_num[first_month]

    for idx, order in enumerate(orders):
        month_abbr, day = order["date"].split()
        month_abbr = month_abbr.title()
        month_num = month_to_num[month_abbr]

        if idx and month_num > prev_month_num:   # crossed past January
            year -= 1

        order["date"] = f"{month_abbr} {day} {year}"
        prev_month_num = month_num

    return orders

def get_best_comparison(amount):
    """
    Auto-selects the most impactful comparison category and item.
    Returns the comparison that results in the most relatable quantity (1-10 range preferred).
    """
    
    comparisons = {
        'grocery': {
            'lattes': {'price': 6, 'name': 'Starbucks lattes ☕'},
            'groceries_week': {'price': 200, 'name': 'weeks of groceries 🛒'},
            'groceries_month': {'price': 800, 'name': 'months of groceries 🛒'}
        },
        'experience': {
            'movies': {'price': 15, 'name': 'movie tickets 🎬'},
            'dinners': {'price': 80, 'name': 'nice dinners out 🍽️'},
            'weekends': {'price': 600, 'name': 'weekend getaways ✈️'},
            'vacations': {'price': 2000, 'name': 'week-long vacations 🏖️'}
        },
        'tech': {
            'airpods': {'price': 180, 'name': 'AirPods 🎧'},
            'watches': {'price': 400, 'name': 'Apple Watches ⌚'},
            'iphones': {'price': 1000, 'name': 'iPhones 📱'},
            'macbooks': {'price': 1800, 'name': 'MacBooks 💻'}
        }
    }
    
    best_comparison = None
    best_score = float('inf')
    
    # Find the comparison that gives the most relatable quantity (closest to 2-5 range)
    for category in comparisons:
        for _, details in comparisons[category].items():
            quantity = amount / details['price']
            # Score based on how close to ideal range (2-5), with preference for experiences > tech > grocery
            if 1 <= quantity <= 10:
                category_bonus = {'experience': 0, 'tech': 0.5, 'grocery': 1}[category]
                score = abs(quantity - 3) + category_bonus  # Prefer quantity around 3
                if score < best_score:
                    best_score = score
                    best_comparison = {
                        'quantity': f'{quantity:.1f}' if quantity < 10 else f'{quantity:.0f}',
                        'description': details['name']
                    }
    
    # Fallback if no good match found
    if not best_comparison:
        if amount < 100:
            best_comparison = {'quantity': f'{amount/4:.0f}', 'description': 'Starbucks lattes ☕'}
        else:
            best_comparison = {'quantity': f'{amount/80:.1f}', 'description': 'nice dinners out 🍽️'}
    
    return best_comparison



def upload_chart_to_s3(fig, chart_name: str, timestamp: str) -> str:
    """
    Upload a matplotlib figure to S3 and return the public URL.
    
    Args:
        fig: matplotlib figure object
        chart_name: name for the chart file (e.g., 'spending', 'cumulative')
        timestamp: timestamp string for unique filename
        
    Returns:
        Public URL of the uploaded chart or None if upload failed
    """
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches='tight', 
                    facecolor='white', edgecolor='none')
        buf.seek(0)
        
        chart_key = f"charts/{timestamp}_{chart_name}_chart.png"
        bucket_name = os.environ.get('CHARTS_BUCKET_NAME', 'ubereats-orders-bucket')
        
        s3_client.put_object(
            Bucket=bucket_name,
            Key=chart_key,
            Body=buf.getvalue(),
            ContentType='image/png'
        )
        
        chart_url = f"https://{bucket_name}.s3.amazonaws.com/{chart_key}"
        buf.close()
        
        logger.info(f"Successfully uploaded {chart_name} chart to S3: {chart_url}")
        return chart_url
        
    except Exception as e:
        logger.error(f"Error uploading {chart_name} chart to S3: {str(e)}")
        return None

def analyze_orders(orders: List[Dict[str, Any]]) -> str:
    """
    Analyze Uber Eats orders and generate HTML email body.
    
    Args:
        orders: List of order dictionaries with keys: restaurantName, date, time, total, canceled
        
    Returns:
        HTML string for email body
    """
    if not orders:
        return "<html><body><h1>No orders found to analyze</h1></body></html>"
    
    # Process the data
    df = (
        pd.DataFrame(add_years_to_orders(orders))
          .assign(
              total_value=lambda d: pd.to_numeric(d.total.str.replace("$", "")),
              ts=lambda d: pd.to_datetime(d["date"] + " " + d["time"]),
          )
          .sort_values("ts", ascending=True)      # chronological for plotting
          .assign(
              cum_value=lambda d: d.total_value.cumsum().ffill(),
          )
          .reset_index(drop=True)
    )

    monthly_spend = df.groupby(df["ts"].dt.to_period("M")).total_value.sum()

    # --- 3.  KPIs ----------------------------------------------------------------
    total_orders: int = len(df)
    total_spent: float = df["total_value"].sum()
    largest_order_row = df.loc[df["total_value"].idxmax()]
    top_hour = _hour_label(
        df["ts"].dt.hour
          .value_counts()
          .idxmax()
    )
    top_day = df["ts"].dt.day_name().value_counts().idxmax()
    canceled_orders = len(df[df["canceled"] == True])
    top_restaurant = df["restaurantName"].value_counts().idxmax()
    top_restaurant_count = df["restaurantName"].value_counts().max()
    avg_order_value = df["total_value"].mean()

    # Set modern style
    plt.style.use('seaborn-v0_8-whitegrid')
    sns.set_palette("husl")

    # --- 4. Spending over time chart -------------------------------------------
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('white')

    # Create gradient bar chart
    bars = monthly_spend.plot.bar(ax=ax, color='#667eea', alpha=0.8, 
                                  edgecolor='white', linewidth=0.5)

    ax.set_xlabel("Order Time", fontsize=12, color='#2c3e50', fontweight='500')
    ax.set_ylabel("Order Total ($)", fontsize=12, color='#2c3e50', fontweight='500')
    ax.set_title("Uber Eats Spending Over Time", fontsize=16, color='#2c3e50', 
                 fontweight='600', pad=20)

    # Style the axes
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#ecf0f1')
    ax.spines['bottom'].set_color('#ecf0f1')
    ax.tick_params(colors='#7f8c8d', labelsize=10)
    ax.grid(True, alpha=0.3, color='#bdc3c7')

    # Format y-axis as currency
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))

    plt.xticks(rotation=45, ha='right')
    fig.tight_layout()

    # Generate unique timestamp for charts
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Upload spending chart to S3
    spending_chart_url = upload_chart_to_s3(fig, 'spending', timestamp)
    plt.close(fig)  # Free memory

    # --- 5. Cumulative spending chart -------------------------------------------
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('white')

    # Create smooth line with gradient fill
    ax.plot(df["ts"], df["cum_value"], linewidth=3, color='#764ba2', 
            marker='o', markersize=4, markerfacecolor='white', 
            markeredgecolor='#764ba2', markeredgewidth=2)

    # Add gradient fill under the line
    ax.fill_between(df["ts"], df["cum_value"], alpha=0.3, color='#764ba2')

    ax.set_xlabel("Order Time", fontsize=12, color='#2c3e50', fontweight='500')
    ax.set_ylabel("Cumulative Order Total ($)", fontsize=12, color='#2c3e50', fontweight='500')
    ax.set_title("Uber Eats Cumulative Spending Over Time", fontsize=16, color='#2c3e50', 
                 fontweight='600', pad=20)

    # Style the axes
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#ecf0f1')
    ax.spines['bottom'].set_color('#ecf0f1')
    ax.tick_params(colors='#7f8c8d', labelsize=10)
    ax.grid(True, alpha=0.3, color='#bdc3c7')

    # Format y-axis as currency
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))

    plt.xticks(rotation=45, ha='right')
    fig.tight_layout()

    # Upload cumulative chart to S3
    cumulative_chart_url = upload_chart_to_s3(fig, 'cumulative', timestamp)
    plt.close(fig)  # Free memory

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                margin: 0;
                padding: 20px;
                color: #333;
            }}
            .container {{
                max-width: 800px;
                margin: 0 auto;
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 16px;
                padding: 40px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
            }}
            h2 {{
                color: #2c3e50;
                font-size: 32px;
                font-weight: 700;
                margin: 0 0 30px 0;
                text-align: center;
                background: linear-gradient(45deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }}
            .stats-grid {{
                margin-bottom: 40px;
            }}
            .stats-row {{
                width: 100%;
                margin-bottom: 20px;
            }}
            .stats-row table {{
                width: 100%;
                border-collapse: separate;
                border-spacing: 10px;
                table-layout: fixed;
            }}
            .stat-card {{
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
                border-left: 4px solid #667eea;
                width: 33.33%;
                vertical-align: top;
            }}
            .stat-label {{
                font-size: 14px;
                color: #7f8c8d;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                margin-bottom: 8px;
            }}
            .stat-value {{
                font-size: 24px;
                font-weight: 700;
                color: #2c3e50;
            }}
            .chart-container {{
                margin: 30px 0;
                text-align: center;
                padding: 20px;
                background: white;
                border-radius: 12px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            }}
            .chart-container img {{
                max-width: 100%;
                height: auto;
                border-radius: 8px;
                box-shadow: 0 8px 30px rgba(0, 0, 0, 0.12);
                transition: transform 0.2s ease;
            }}
            .chart-container img:hover {{
                transform: scale(1.02);
            }}
            .chart-container p {{
                margin: 0;
                font-size: 16px;
            }}
            h3 {{
                color: #2c3e50;
                font-size: 24px;
                font-weight: 600;
                margin: 40px 0 20px 0;
                text-align: center;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 12px;
                overflow: hidden;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            }}
            th {{
                background: linear-gradient(45deg, #667eea, #764ba2);
                color: white;
                padding: 15px;
                text-align: left;
                font-weight: 600;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #ecf0f1;
            }}
            tr:last-child td {{
                border-bottom: none;
            }}
            tr:nth-child(even) {{
                background: #f8f9fa;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🍔 Your Uber Eats Summary</h2>
            
            <div class="stats-grid">
                <div class="stats-row">
                    <table>
                        <tr>
                            <td class="stat-card">
                                <div class="stat-label">Total Spent</div>
                                <div class="stat-value">${total_spent:,.2f}</div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Average Order Cost</div>
                                <div class="stat-value">${avg_order_value:.2f}</div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Total Orders</div>
                                <div class="stat-value">{total_orders}</div>
                            </td>
                        </tr>
                    </table>
                </div>
                <div class="stats-row">
                    <table>
                        <tr>
                            <td class="stat-card">
                                <div class="stat-label">Cancelled Orders</div>
                                <div class="stat-value">{canceled_orders}</div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Peak Ordering Hour</div>
                                <div class="stat-value">{top_hour}</div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Top Day to Order</div>
                                <div class="stat-value">{top_day}</div>
                            </td>
                        </tr>
                    </table>
                </div>
                <div class="stats-row">
                    <table>
                        <tr>
                            <td class="stat-card">
                                <div class="stat-label">Top Restaurant</div>
                                <div class="stat-value">{top_restaurant}</div>
                                <div style="font-size: 12px; color: #7f8c8d; margin-top: 5px;">
                                    Ordered {top_restaurant_count} times
                                </div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Largest Order</div>
                                <div class="stat-value">${largest_order_row['total_value']:.2f}</div>
                                <div style="font-size: 12px; color: #7f8c8d; margin-top: 5px;">
                                    {largest_order_row['restaurantName']} ({largest_order_row['date']})
                                </div>
                            </td>
                            <td class="stat-card">
                                <div class="stat-label">Could Have Bought</div>
                                <div class="stat-value">{get_best_comparison(total_spent)['quantity']}</div>
                                <div style="font-size: 12px; color: #7f8c8d; margin-top: 5px;">
                                    {get_best_comparison(total_spent)['description']}
                                </div>
                            </td>
                        </tr>
                    </table>
                </div>
            </div>

            <div class="chart-container">
                {f'<img src="{spending_chart_url}" alt="Spending Over Time Chart - Shows your Uber Eats spending patterns over time" style="display: block; margin: 0 auto; max-width: 100%; height: auto;"/>' if spending_chart_url else f'<div style="text-align: center; color: #7f8c8d; padding: 20px;"><p>📊 Monthly Spending Summary</p><div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 10px 0;">{monthly_spend.to_frame("Amount").to_html(classes="", table_id="", escape=False)}</div></div>'}
            </div>

            <div class="chart-container">
                {f'<img src="{cumulative_chart_url}" alt="Cumulative Spending Chart - Shows your total Uber Eats spending growth over time" style="display: block; margin: 0 auto; max-width: 100%; height: auto;"/>' if cumulative_chart_url else '<p style="text-align: center; color: #7f8c8d; padding: 20px;">📈 See your order history table below for spending progression</p>'}
            </div>

            <h3>📋 Order Details</h3>
            {df[['restaurantName','date','time','total']].to_html(index=False)}
        </div>
    </body>
    </html>
    """
    
    return html_body

def send_email(to_email: str, subject: str, content: str) -> bool:
    """
    Send an HTML email using SendGrid to a single recipient.
    """
    try:
        # Get SendGrid API key and sender email from environment variables
        sendgrid_api_key = os.environ.get('DEREK_SENDGRID_API_KEY')
        sender_email = os.environ.get('DEREK_SENDER_EMAIL')
        
        if not sendgrid_api_key:
            raise ValueError("DEREK_SENDGRID_API_KEY environment variable is not set")
        if not sender_email:
            raise ValueError("DEREK_SENDER_EMAIL environment variable is not set")
        
        # Create message with HTML content
        message = Mail(
            from_email=Email(sender_email),
            to_emails=To(to_email),
            subject=subject,
            html_content=Content("text/html", content)
        )
        
        # Send email
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        
        logger.info(f"Email sent to {to_email} with status code: {response.status_code}")
        return response.status_code >= 200 and response.status_code < 300
    
    except Exception as e:
        logger.error(f"Error sending email to {to_email}: {str(e)}")
        return False

def extract_user_email_from_key(s3_key: str) -> str:
    """
    Extract user email from S3 key. Assumes format like: 
    orders/{user_email}/orders.json or similar
    """
    parts = s3_key.split('/')
    for part in parts:
        if '@' in part and '.' in part:
            return part
    
    # Fallback - return a portion of the key as identifier
    return parts[-2] if len(parts) > 1 else 'unknown'

def lambda_handler(event, context):
    try:
        logger.info(f"Received event: {json.dumps(event)}")
        
        # Process each record in the event
        for record in event.get('Records', []):
            # Check if this is an S3 event
            if record.get('eventSource') == 'aws:s3':
                # Get bucket and key information
                source_bucket = record['s3']['bucket']['name']
                source_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
                
                logger.info(f"Processing file: {source_key} from bucket: {source_bucket}")
                
                # Skip non-JSON files
                if not source_key.lower().endswith('.json'):
                    logger.info(f"Skipping non-JSON file: {source_key}")
                    continue
                
                # Extract user email from S3 key
                user_email = extract_user_email_from_key(source_key)
                logger.info(f"Processing orders for user: {user_email}")
                
                try:
                    # Download JSON file from S3
                    response = s3_client.get_object(Bucket=source_bucket, Key=source_key)
                    file_content = response['Body'].read().decode('utf-8')
                    
                    # Parse JSON data
                    orders_data = json.loads(file_content)
                    
                    # Handle different JSON structures
                    if isinstance(orders_data, dict) and 'orders' in orders_data:
                        orders = orders_data['orders']
                    elif isinstance(orders_data, list):
                        orders = orders_data
                    else:
                        logger.error(f"Unexpected JSON structure in {source_key}")
                        continue
                    
                    logger.info(f"Found {len(orders)} orders to analyze")
                    
                    if not orders:
                        logger.warning(f"No orders found in {source_key}")
                        continue
                    
                    # Perform analysis and generate HTML
                    html_content = analyze_orders(orders)
                    
                    # Send email with analysis
                    subject = f"🍔 Your Uber Eats Analysis - {len(orders)} Orders Analyzed"
                    
                    success = send_email(
                        to_email=user_email,
                        subject=subject,
                        content=html_content
                    )
                    
                    if success:
                        logger.info(f"Successfully sent analysis email to {user_email}")
                    else:
                        logger.error(f"Failed to send email to {user_email}")
                        
                except ClientError as e:
                    logger.error(f"Error accessing S3 object {source_key}: {str(e)}")
                    continue
                except json.JSONDecodeError as e:
                    logger.error(f"Error parsing JSON from {source_key}: {str(e)}")
                    continue
                except Exception as e:
                    logger.error(f"Error processing {source_key}: {str(e)}")
                    continue
        
        return {
            'statusCode': 200,
            'body': json.dumps('Uber Eats analysis completed successfully')
        }
    
    except Exception as e:
        logger.error(f"Error in lambda_handler: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }