import pandas as pd
import numpy as np
from typing import Dict, Tuple
from utils.logger import log_info


class StructureAnalyzer:
    """Efficient break of structure analysis for forex trading."""
    
    # Symbol-specific pip values
    PIP_VALUES = {
        'XAU': 0.10,    # Gold pairs
        'JPY': 0.01,    # Japanese Yen pairs
        'DEFAULT': 0.0001  # Major pairs
    }
    
    def __init__(self, swing_strength: int = 2, min_structure_pips: float = 3.0):
        self.swing_strength = swing_strength
        self.min_structure_pips = min_structure_pips
    
    def _get_pip_value(self, symbol: str) -> float:
        """Get pip value based on symbol type."""
        symbol = symbol.upper()
        if symbol.startswith('XAU'):
            return self.PIP_VALUES['XAU']
        elif 'JPY' in symbol:
            return self.PIP_VALUES['JPY']
        return self.PIP_VALUES['DEFAULT']
    
    def find_swing_points(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect swing highs and lows with proper boundary checks and validation
        """ 
        df = pd.DataFrame(df)
        df['swing_high'] = False
        df['swing_low'] = False
        df['swing_high_price'] = np.nan
        df['swing_low_price'] = np.nan
        
        if len(df) < 2 * self.swing_strength + 1:
            print(f"Warning: Not enough data points. Need at least {2 * self.swing_strength + 1}, got {len(df)}")
            return df
        
        # Detect swing points
        for i in range(self.swing_strength, len(df) - self.swing_strength):
            # Check if current bar is highest in window
            is_high = all(df.iloc[i]['high'] > df.iloc[i - j]['high'] and 
                         df.iloc[i]['high'] > df.iloc[i + j]['high']
                         for j in range(1, self.swing_strength + 1))
            
            # Check if current bar is lowest in window
            is_low = all(df.iloc[i]['low'] < df.iloc[i - j]['low'] and 
                        df.iloc[i]['low'] < df.iloc[i + j]['low']
                        for j in range(1, self.swing_strength + 1))
            
            if is_high:
                df.at[i, 'swing_high'] = True
                df.at[i, 'swing_high_price'] = df.iloc[i]['high']
            
            if is_low:
                df.at[i, 'swing_low'] = True
                df.at[i, 'swing_low_price'] = df.iloc[i]['low']
        
        return df
    
    def analyze_structure_break(self, df: pd.DataFrame, direction: str, 
                              symbol: str = "EURUSD") -> Dict:
        """
        Analyze break of structure with comprehensive validation.
        
        Args:
            df: OHLC dataframe with columns ['open', 'high', 'low', 'close']
            direction: 'bullish' or 'bearish'
            symbol: Trading symbol for pip calculation
            
        Returns:
            Dictionary with analysis results
        """
        if len(df) < self.swing_strength * 4:
            return {
                'confirmed': False,
                'reason': f'Insufficient data (need >{self.swing_strength * 4} bars)',
                'confidence': 0.0
            }
        
        # Validate direction
        direction = direction.lower()
        if direction not in ['bullish', 'bearish']:
            return {
                'confirmed': False,
                'reason': 'Direction must be "bullish" or "bearish"',
                'confidence': 0.0
            }
        
        # Find swing points
        data_with_swings = self.find_swing_points(df)
        current_close = data_with_swings['close'].iloc[-1]
        min_break_distance = self._get_pip_value(symbol) * self.min_structure_pips
        
        # Get relevant swing points
        if direction == 'bullish':
            swing_points = data_with_swings[data_with_swings['swing_high']].copy()
            if swing_points.empty:
                return {
                    'confirmed': False,
                    'reason': 'No swing highs found for bullish break',
                    'confidence': 0.0
                }
            
            # Use most recent swing high
            last_swing = swing_points.iloc[-1]
            break_level = last_swing['swing_high_price']
            required_break_price = break_level + min_break_distance
            confirmed = current_close > required_break_price
            break_distance_pips = (current_close - break_level) / self._get_pip_value(symbol)
            
        else:  # bearish
            swing_points = data_with_swings[data_with_swings['swing_low']].copy()
            if swing_points.empty:
                return {
                    'confirmed': False,
                    'reason': 'No swing lows found for bearish break',
                    'confidence': 0.0
                }
            
            # Use most recent swing low
            last_swing = swing_points.iloc[-1]
            break_level = last_swing['swing_low_price']
            required_break_price = break_level - min_break_distance
            confirmed = current_close < required_break_price
            break_distance_pips = (break_level - current_close) / self._get_pip_value(symbol)
        
        # Calculate confidence based on break strength
        confidence = min(1.0, max(0.0, abs(break_distance_pips) / 10.0)) if confirmed else 0.0
        
        return {
            'confirmed': confirmed,
            'direction': direction.capitalize(),
            'break_level': break_level,
            'structure_level': break_level,  # Legacy field for compatibility
            'current_close': current_close,
            'break_distance_pips': break_distance_pips,
            'min_required_pips': self.min_structure_pips,
            'confidence': confidence,
            'swing_count': len(swing_points),
            'bars_since_swing': len(data_with_swings) - last_swing.name - 1,
            'reason': 'Break confirmed' if confirmed else 'Break not confirmed'
        }
    
    def calculate_adaptive_risk(self, analysis_result: Dict, base_risk: float,
                              min_risk: float, max_risk: float) -> float:
        """
        Calculate adaptive risk based on break of structure strength.
        
        Args:
            analysis_result: Result from analyze_structure_break
            base_risk: Base risk percentage
            min_risk: Minimum risk percentage
            max_risk: Maximum risk percentage
            
        Returns:
            Adjusted risk percentage
        """
        if not analysis_result['confirmed']:
            return base_risk
        
        confidence = analysis_result['confidence']
        break_distance = abs(analysis_result['break_distance_pips'])
        
        # Risk adjustment based on break strength and confidence
        if break_distance > 15 and confidence > 0.8:
            return max_risk
        elif break_distance > 8 and confidence > 0.6:
            return base_risk + (max_risk - base_risk) * 0.7
        elif break_distance > 5 and confidence > 0.4:
            return base_risk + (max_risk - base_risk) * 0.4
        else:
            return max(min_risk, base_risk * confidence)


# Convenience functions for strategy.py integration
def confirm_break_of_structure(df: pd.DataFrame, direction: str, symbol: str = "EURUSD",
                               swing_strength: int = 2, min_structure_size: float = 1.0) -> Dict:
    """
    Complete break of structure analysis including swing point detection.
    
    Args:
        df: OHLC dataframe with columns ['open', 'high', 'low', 'close']
        direction: 'bullish' or 'bearish'
        symbol: Trading symbol for pip calculation
        swing_strength: Number of candles on each side needed to confirm a swing
        min_structure_size: Minimum structure size in pips
        
    Returns:
        Dictionary with comprehensive analysis results
    """
    
    if direction is None:
        last_candle = df[-1]  # The most recent candle

        if last_candle["close"] > last_candle["open"]:
            direction = 'bullish'
        elif last_candle["close"] < last_candle["open"]:
            direction = 'bearish'
        else:
            direction = "bullish"  # Default to bullish if no clear direction

    
    analyzer = StructureAnalyzer(swing_strength, min_structure_size)
    return analyzer.analyze_structure_break(df, direction, symbol)


def adaptive_risk_bos(df: pd.DataFrame, direction: str, symbol: str, 
                     base_risk: float, min_risk: float, max_risk: float,
                     swing_strength: int = 2, min_structure_size: float = 1.0) -> Tuple[bool, float, Dict]:
    """
    Complete adaptive risk calculation with integrated break of structure analysis.
    
    Args:
        data: OHLC dataframe with columns ['open', 'high', 'low', 'close']
        direction: 'bullish' or 'bearish'
        symbol: Trading symbol for pip calculation
        base_risk: Base risk percentage
        min_risk: Minimum risk percentage
        max_risk: Maximum risk percentage
        swing_strength: Number of candles on each side needed to confirm a swing
        min_structure_size: Minimum structure size in pips
        
    Returns:
        Tuple of (confirmed, adjusted_risk, analysis_dict)
    """
    
    if direction is None:
        last_candle = df[-1]  # The most recent candle

        if last_candle["close"] > last_candle["open"]:
            direction = 'bullish'
        elif last_candle["close"] < last_candle["open"]:
            direction = 'bearish'
        else:
            direction = "bullish"  # Default to bullish if no clear direction

    
    analyzer = StructureAnalyzer(swing_strength, min_structure_size)
    
    # Get full break of structure analysis
    bos_result = analyzer.analyze_structure_break(df, direction, symbol)
    
    if not bos_result['confirmed']:
        return False, base_risk, bos_result
    
    # Calculate adaptive risk
    adjusted_risk = analyzer.calculate_adaptive_risk(bos_result, base_risk, min_risk, max_risk)
    
    return True, adjusted_risk, bos_result