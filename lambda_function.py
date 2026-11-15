import json
import boto3
import csv
import math

s3_client = boto3.client('s3')

BUCKET_NAME = "poly-dailyprices-csv"  
FILE_KEY = "prices_daily.csv"         

def lambda_handler(event, context):
    try:
        parameters = event.get('parameters', [])
        risk_profile = "medium"
        horizon_years = 5
        
        for param in parameters:
            if param['name'] == 'risk_profile':
                risk_profile = param['value'].lower()
            if param['name'] == 'horizon_years':
                horizon_years = int(param['value'])

        response = s3_client.get_object(Bucket=BUCKET_NAME, Key=FILE_KEY)
        csv_text = response['Body'].read().decode('utf-8').splitlines()
        reader = csv.reader(csv_text)
        
        headers = [h.strip().lower() for h in next(reader)]
        ticker_idx = 1
        price_idx = 6  

        asset_histories = {}
        for row in reader:
            if not row or len(row) <= max(ticker_idx, price_idx): 
                continue
            ticker_name = row[ticker_idx].strip().upper()
            price_str = row[price_idx].strip()
            
            if ticker_name and price_str:
                try:
                    price_val = float(price_str)
                    if ticker_name not in asset_histories:
                        asset_histories[ticker_name] = []
                    asset_histories[ticker_name].append(price_val)
                except ValueError:
                    pass

        metrics = {}
        for name, prices in asset_histories.items():
            if len(prices) < 5: 
                continue 
            
            daily_returns = []
            for i in range(1, len(prices)):
                if prices[i-1] != 0:
                    daily_returns.append((prices[i] - prices[i-1]) / prices[i-1])
            
            if not daily_returns: 
                continue
            
            # Basic asset metrics
            mean_return = sum(daily_returns) / len(daily_returns)
            ann_return = mean_return * 252 
            variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
            ann_vol = math.sqrt(variance) * math.sqrt(252)
            
            # Sortino Downside Deviation (only looking at returns below zero)
            downside_returns = [r for r in daily_returns if r < 0]
            if downside_returns:
                downside_variance = sum(r ** 2 for r in downside_returns) / len(daily_returns)
                ann_downside_std = math.sqrt(downside_variance) * math.sqrt(252)
            else:
                ann_downside_std = ann_vol
            
            # Compound Annual Growth Rate (CAGR) calculation over the series length
            total_return_factor = prices[-1] / prices[0] if prices[0] != 0 else 1
            years_elapsed = len(prices) / 252.0
            cagr_val = (total_return_factor ** (1.0 / years_elapsed)) - 1 if years_elapsed > 0 else ann_return

            if ann_vol > 0:
                metrics[name] = {
                    'return': ann_return,
                    'volatility': ann_vol,
                    'cagr': cagr_val,
                    'sharpe': (ann_return - 0.04) / ann_vol,
                    'sortino': (ann_return - 0.04) / ann_downside_std if ann_downside_std > 0 else 0
                }

        if not metrics:
            raise ValueError("No historical asset validation data built.")

        recommended_portfolio = {}
        benchmark = "S&P 500"
        sorted_by_vol = sorted(metrics.keys(), key=lambda x: metrics[x]['volatility'])
        
        if risk_profile == "low":
            selected = sorted_by_vol[:4]
            weight = 1.0 / len(selected)
            recommended_portfolio = {asset: weight for asset in selected}
            benchmark = "SOFR + 4%"
        elif risk_profile == "high":
            high_vol_pool = sorted_by_vol[-10:]
            selected = sorted(high_vol_pool, key=lambda x: metrics[x]['return'], reverse=True)[:4]
            total_return_score = sum(metrics[a]['return'] for a in selected)
            if total_return_score > 0:
                recommended_portfolio = {a: (metrics[a]['return']/total_return_score) for a in selected}
            else:
                recommended_portfolio = {a: (1.0/len(selected)) for a in selected}
        else: # Medium / Balanced
            selected = sorted(metrics.keys(), key=lambda x: metrics[x]['sharpe'], reverse=True)[:4]
            inv_vols = [1.0 / metrics[a]['volatility'] for a in selected]
            total_inv_vol = sum(inv_vols)
            for idx, asset in enumerate(selected):
                recommended_portfolio[asset] = inv_vols[idx] / total_inv_vol

        # Compute Aggregated Weighted Portfolio Metrics Natively
        p_cagr = sum(metrics[a]['cagr'] * w for a, w in recommended_portfolio.items())
        p_sharpe = sum(metrics[a]['sharpe'] * w for a, w in recommended_portfolio.items())
        p_sortino = sum(metrics[a]['sortino'] * w for a, w in recommended_portfolio.items())
        p_vol = sum(metrics[a]['volatility'] * w for a, w in recommended_portfolio.items())

        portfolio_weights_formatted = {a: f"{w:.2%}" for a, w in recommended_portfolio.items()}

        response_body = {
            "TEXT": json.dumps({
                "status": "success",
                "risk_profile_processed": risk_profile,
                "horizon_years": horizon_years,
                "benchmark_set": benchmark,
                "recommended_portfolio": portfolio_weights_formatted,
                "portfolio_summary_metrics": {
                    "cagr": f"{p_cagr:.2%}",
                    "sharpe_ratio": f"{p_sharpe:.2f}",
                    "sortino_ratio": f"{p_sortino:.2f}",
                    "annualized_volatility": f"{p_vol:.2%}"
                },
                "msg": "Advanced risk-adjusted evaluation executed natively."
            })
        }
        
        return {
            'actionGroup': event.get('actionGroup', 'PortfolioEngine'),
            'function': event.get('function', 'optimize'),
            'functionResponse': {'responseBody': response_body}
        }

    except Exception as e:
        return {
            'actionGroup': event.get('actionGroup', 'PortfolioEngine'),
            'function': event.get('function', 'optimize'),
            'functionResponse': {
                'responseBody': {"TEXT": json.dumps({"status": "error", "message": str(e)})}
            }
        }
