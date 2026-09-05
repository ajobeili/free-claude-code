"""Tests for Product Analyzer."""

import pytest

from src.sales_engine.core.product_analyzer import ProductAnalyzer, ProductInfo, FileAnalysis


@pytest.mark.asyncio
async def test_analyze_product_creates_info():
    """Test that analyzing a product creates ProductInfo."""
    analyzer = ProductAnalyzer()

    # Use a mock file path
    result = await analyzer.analyze_product(
        file_path="/tmp/test-product.pdf",
        file_type="PDF",
    )

    assert result.file_name == "/tmp/test-product.pdf"
    assert result.file_type == "PDF"
    # Note: actual AI analysis requires valid API key


@pytest.mark.asyncio
async def test_extract_text_returns_something():
    """Test that text extraction returns content placeholder."""
    analyzer = ProductAnalyzer()

    text = analyzer._extract_text("/tmp/test.pdf", "PDF")
    assert text is not None
    assert "test.pdf" in text