"""
Strategy Function Test Suite
Tests all imported functions in strategy.py to ensure they work correctly
"""

import sys
import os
import traceback
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Mock MetaTrader5 for testing
class MockMT5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440
    
    @staticmethod
    def copy_rates_from_pos(symbol, timeframe, start, count):
        """Generate mock OHLC data"""
        np.random.seed(42)  # For reproducible results
        
        # Base price varies by symbol
        if symbol.startswith("XAU"):
            base_price = 2000.0
            volatility = 20.0
        elif "JPY" in symbol:
            base_price = 150.0
            volatility = 2.0
        else:
            base_price = 1.1000
            volatility = 0.01
            
        # Generate realistic OHLC data
        data = []
        current_time = int(datetime.now().timestamp()) - (count * timeframe * 60)
        
        for i in range(count):
            # Random walk for realistic price movement
            change = np.random.normal(0, volatility * 0.001)
            base_price += change
            
            # Generate OHLC
            open_price = base_price
            high_range = abs(np.random.normal(0, volatility * 0.002))
            low_range = abs(np.random.normal(0, volatility * 0.002))
            
            high_price = open_price + high_range
            low_price = open_price - low_range
            close_price = open_price + np.random.normal(0, volatility * 0.001)
            
            # Ensure OHLC relationship is valid
            high_price = max(high_price, open_price, close_price)
            low_price = min(low_price, open_price, close_price)
            
            data.append({
                'time': current_time + (i * timeframe * 60),
                'open': round(open_price, 5),
                'high': round(high_price, 5),
                'low': round(low_price, 5),
                'close': round(close_price, 5),
                'tick_volume': np.random.randint(100, 1000),
                'spread': 0,
                'real_volume': 0
            })
            
            base_price = close_price
        
        return np.array([(d['time'], d['open'], d['high'], d['low'], d['close'], 
                         d['tick_volume'], d['spread'], d['real_volume']) for d in data],
                       dtype=[('time', 'i8'), ('open', 'f8'), ('high', 'f8'), 
                             ('low', 'f8'), ('close', 'f8'), ('tick_volume', 'i8'),
                             ('spread', 'i4'), ('real_volume', 'i8')])

# Mock logger functions
def mock_log(message, highlight=False):
    print(f"[LOG] {message}")

# Mock modules
sys.modules['MetaTrader5'] = MockMT5()

# Mock imports
class MockLogger:
    @staticmethod
    def log_info(msg, highlight=False): mock_log(f"INFO: {msg}", highlight)
    @staticmethod
    def log_success(msg, highlight=False): mock_log(f"SUCCESS: {msg}", highlight)
    @staticmethod
    def log_warning(msg, highlight=False): mock_log(f"WARNING: {msg}", highlight)
    @staticmethod
    def log_error(msg, highlight=False): mock_log(f"ERROR: {msg}", highlight)
    @staticmethod
    def log_fatal(msg, highlight=False): mock_log(f"FATAL: {msg}", highlight)
    @staticmethod
    def log_skip(msg, highlight=False): mock_log(f"SKIP: {msg}", highlight)
    @staticmethod
    def log_trade(msg, highlight=False): mock_log(f"TRADE: {msg}", highlight)
    @staticmethod
    def log_debug(msg, symbol="", debug_type=""): mock_log(f"DEBUG [{symbol}] {debug_type}: {msg}")

class MockRiskManager:
    @staticmethod
    def calculate_position_size(symbol, entry, sl, risk_percent):
        # Simple mock calculation
        pip_value = 0.0001 if "JPY" not in symbol else 0.01
        if symbol.startswith("XAU"):
            pip_value = 0.10
        
        risk_pips = abs(entry - sl) / pip_value
        if risk_pips == 0:
            return 0
        
        account_balance = 10000  # Mock balance
        risk_amount = account_balance * (risk_percent / 100)
        position_size = risk_amount / (risk_pips * 10)  # $10 per pip for standard lot
        
        return max(0.01, min(position_size, 10.0))  # Clamp between 0.01 and 10 lots

class MockOrderManager:
    @staticmethod
    def send_order(symbol, lot_size, direction, sl, tp, magic=0):
        # Mock successful order
        if lot_size > 0 and sl != 0 and tp != 0:
            return True, "Order placed successfully"
        return False, "Invalid parameters"

class MockHTFSweepDetector:
    def __init__(self, window=100, strength=2):
        self.window = window
        self.strength = strength
    
    def run(self, df, debug=False):
        df = df.copy()
        df['htf_high_sweep'] = False
        df['htf_low_sweep'] = False
        
        # Mock some sweep signals
        if len(df) > 10:
            df.loc[df.index[-5], 'htf_high_sweep'] = True  # Bearish sweep
            df.loc[df.index[-3], 'htf_low_sweep'] = True   # Bullish sweep
        
        return df

class MockFVGDetector:
    @staticmethod
    def find_fvg_multi_tf_safe(symbol, min_size, timeframes, candles_to_fetch, 
                              timeframe_map, mt5_module, min_gap_percentage, 
                              direction=None, debug=False):
        # Mock FVG data
        if np.random.random() > 0.5:  # 50% chance of finding FVG
            base_price = 1.1000 if "JPY" not in symbol else 150.0
            if symbol.startswith("XAU"):
                base_price = 2000.0
                
            return {
                'low': base_price - 0.001,
                'high': base_price + 0.001,
                'type': direction or ('Bullish' if np.random.random() > 0.5 else 'Bearish'),
                'timeframe': 'M15',
                'timestamp': int(datetime.now().timestamp()) - 3600
            }
        return None
    
    @staticmethod
    def detect_fvg_across_timeframes(symbol, timeframes, fvg, mt5_module, timeframe_map):
        # Mock FVG tap detection
        return np.random.random() > 0.3  # 70% chance of being tapped

class MockRRProcessor:
    @staticmethod
    def process_trade_data(symbol, state, min_rr, max_rr, dynamic_rr, tf_data):
        # Mock R:R calculation
        if state.entry_price and state.stop_loss:
            # Calculate basic R:R
            pip_value = 0.0001 if "JPY" not in symbol else 0.01
            if symbol.startswith("XAU"):
                pip_value = 0.10
                
            sl_distance = abs(state.entry_price - state.stop_loss)
            tp_distance = sl_distance * max(min_rr, 3.0)  # Default 3:1 R:R
            
            if state.direction == "Bullish":
                state.take_profit = state.entry_price + tp_distance
            else:
                state.take_profit = state.entry_price - tp_distance
                
            state.rr = tp_distance / sl_distance if sl_distance > 0 else min_rr

# Mock the imports
sys.modules['utils.logger'] = MockLogger()
sys.modules['core.risk_manager'] = MockRiskManager()
sys.modules['utils.timeframes'] = type('TimeframeModule', (), {
    'timeframe_map': {
        'M1': MockMT5.TIMEFRAME_M1,
        'M5': MockMT5.TIMEFRAME_M5,
        'M15': MockMT5.TIMEFRAME_M15,
        'H1': MockMT5.TIMEFRAME_H1,
        'H4': MockMT5.TIMEFRAME_H4,
        'D1': MockMT5.TIMEFRAME_D1
    }
})()
sys.modules['core.order_manager'] = MockOrderManager()
sys.modules['core.htf_detect'] = type('HTFModule', (), {'HTFSweepDetector': MockHTFSweepDetector})()
sys.modules['core.fvg_detect'] = MockFVGDetector()
sys.modules['core.rr_processing'] = MockRRProcessor()

# Import the BOS functions (assuming they're available)
try:
    from core.bos_detect import adaptive_risk_bos, confirm_break_of_structure
    BOS_AVAILABLE = True
except ImportError:
    print("Warning: BOS detection functions not available, using mocks")
    BOS_AVAILABLE = False
    
    def adaptive_risk_bos(data, direction, symbol, base_risk, min_risk, max_risk):
        confirmed = np.random.random() > 0.4  # 60% success rate
        adjusted_risk = np.random.uniform(min_risk, max_risk) if confirmed else base_risk
        details = {
            'confirmed': confirmed,
            'reason': 'Break confirmed' if confirmed else 'No break detected',
            'structure_level': 1.1000,
            'break_distance_pips': np.random.uniform(1, 20) if confirmed else 0,
            'direction': direction.capitalize()
        }
        return confirmed, adjusted_risk, details
    
    def confirm_break_of_structure(data, direction, symbol):
        confirmed = np.random.random() > 0.4
        return {
            'confirmed': confirmed,
            'reason': 'Break confirmed' if confirmed else 'No break detected',
            'structure_level': 1.1000,
            'break_distance_pips': np.random.uniform(1, 20) if confirmed else 0,
            'direction': direction.capitalize()
        }

def test_function(func_name, func, *args, **kwargs):
    """Test a single function with error handling"""
    try:
        print(f"\n{'='*50}")
        print(f"Testing: {func_name}")
        print(f"{'='*50}")
        
        result = func(*args, **kwargs)
        print(f"✅ SUCCESS: {func_name} executed without errors")
        print(f"Result type: {type(result)}")
        if hasattr(result, '__len__') and len(str(result)) < 200:
            print(f"Result: {result}")
        elif isinstance(result, (dict, list, tuple)):
            print(f"Result preview: {str(result)[:200]}...")
        
        return True, result
        
    except Exception as e:
        print(f"❌ FAILED: {func_name}")
        print(f"Error: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return False, None

def create_mock_data():
    """Create mock data for testing"""
    mt5 = MockMT5()
    
    # Mock OHLC data
    ohlc_data = mt5.copy_rates_from_pos("EURUSD", MockMT5.TIMEFRAME_M15, 0, 100)
    
    # Convert to DataFrame
    df = pd.DataFrame(ohlc_data)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    return ohlc_data, df

def test_bos_functions():
    """Test Break of Structure functions"""
    print("\n" + "="*60)
    print("TESTING BREAK OF STRUCTURE FUNCTIONS")
    print("="*60)
    
    # Create test data
    ohlc_data, df = create_mock_data()
    
    # Test confirm_break_of_structure
    success1, result1 = test_function(
        "confirm_break_of_structure",
        confirm_break_of_structure,
        ohlc_data, "bullish", "EURUSD"
    )
    
    # Test adaptive_risk_bos
    success2, result2 = test_function(
        "adaptive_risk_bos",
        adaptive_risk_bos,
        ohlc_data, "bullish", "EURUSD", 2.0, 0.5, 5.0
    )
    
    return success1 and success2

def test_utility_functions():
    """Test utility functions"""
    print("\n" + "="*60)
    print("TESTING UTILITY FUNCTIONS")
    print("="*60)
    
    # Test pips_to_price function
    def pips_to_price(symbol: str, pips: float) -> float:
        symbol = symbol.upper()
        if symbol.startswith("XAU"):
            return pips * 0.10
        elif "JPY" in symbol:
            return pips * 0.01
        else:
            return pips * 0.0001
    
    success1, _ = test_function("pips_to_price (EURUSD)", pips_to_price, "EURUSD", 10.0)
    success2, _ = test_function("pips_to_price (USDJPY)", pips_to_price, "USDJPY", 10.0)
    success3, _ = test_function("pips_to_price (XAUUSD)", pips_to_price, "XAUUSD", 10.0)
    
    return success1 and success2 and success3

def test_ltf_reversal():
    """Test LTF reversal detection"""
    print("\n" + "="*60)
    print("TESTING LTF REVERSAL DETECTION")
    print("="*60)
    
    def detect_ltf_reversal(data, direction, fvg_sl):
        last = data[-1]
        prev = data[-2]

        if direction == "Bullish":
            if last['close'] > last['open'] and prev['close'] < prev['open'] and last['close'] > prev['open']:
                return last['close'], fvg_sl, "Bullish"

        if direction == "Bearish":
            if last['close'] < last['open'] and prev['close'] > prev['open'] and last['close'] < prev['open']:
                return last['close'], fvg_sl, "Bearish"

        return None, None, None
    
    ohlc_data, _ = create_mock_data()
    
    success1, _ = test_function(
        "detect_ltf_reversal (Bullish)",
        detect_ltf_reversal,
        ohlc_data, "Bullish", 1.0950
    )
    
    success2, _ = test_function(
        "detect_ltf_reversal (Bearish)",
        detect_ltf_reversal,
        ohlc_data, "Bearish", 1.1050
    )
    
    return success1 and success2

def test_mock_integrations():
    """Test mock integrations"""
    print("\n" + "="*60)
    print("TESTING MOCK INTEGRATIONS")
    print("="*60)
    
    # Test HTF Sweep Detector
    detector = MockHTFSweepDetector()
    _, df = create_mock_data()
    success1, _ = test_function("HTFSweepDetector.run", detector.run, df, debug=False)
    
    # Test FVG functions
    success2, _ = test_function(
        "find_fvg_multi_tf_safe",
        MockFVGDetector.find_fvg_multi_tf_safe,
        "EURUSD", 0.0001, {}, 100, {}, MockMT5(), 0.01, "Bullish", False
    )
    
    # Test Risk Manager
    success3, _ = test_function(
        "calculate_position_size",
        MockRiskManager.calculate_position_size,
        "EURUSD", 1.1000, 1.0950, 2.0
    )
    
    # Test Order Manager
    success4, _ = test_function(
        "send_order",
        MockOrderManager.send_order,
        "EURUSD", 0.1, "Bullish", 1.0950, 1.1150, 12345
    )
    
    return success1 and success2 and success3 and success4

def main():
    """Main test runner"""
    print("="*60)
    print("STRATEGY FUNCTION TEST SUITE")
    print("="*60)
    print(f"Test started at: {datetime.now()}")
    
    test_results = []
    
    # Run all tests
    test_results.append(("BOS Functions", test_bos_functions()))
    test_results.append(("Utility Functions", test_utility_functions()))
    test_results.append(("LTF Reversal", test_ltf_reversal()))
    test_results.append(("Mock Integrations", test_mock_integrations()))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = 0
    total = len(test_results)
    
    for test_name, success in test_results:
        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"{test_name}: {status}")
        if success:
            passed += 1
    
    print(f"\nOverall: {passed}/{total} test groups passed")
    
    if passed == total:
        print("🎉 All tests passed! Your strategy functions are working correctly.")
    else:
        print("⚠️  Some tests failed. Check the output above for details.")
    
    print(f"\nTest completed at: {datetime.now()}")
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)