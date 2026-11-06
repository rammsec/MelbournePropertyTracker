"""
Database & PostGIS Spatial ORM Layer (`src/database.py`).

Provides SQLAlchemy 2.0 + GeoAlchemy2 spatial models, connection pooling,
context-managed sessions, and spatial nearest-neighbor query utilities.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Generator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKTElement
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from config.settings import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base declarative class for all ORM models."""


class Suburb(Base):
    """Victorian suburb with multi-polygon boundary geometry (EPSG:4326)."""

    __tablename__ = "suburbs"
    __table_args__ = (
        UniqueConstraint("suburb_name", "postcode", name="uq_suburb_postcode"),
        Index("idx_suburbs_geom", "boundary_geom", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suburb_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    postcode: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(120), nullable=False)
    lga_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    median_house_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    boundary_geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    properties: Mapped[List["Property"]] = relationship("Property", back_populates="suburb")


class Property(Base):
    """Master cadastre-level property entity with PostGIS Point geometry."""

    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint("land_size_sqm >= 0", name="chk_land_size_non_negative"),
        CheckConstraint("bedrooms >= 0", name="chk_bedrooms_non_negative"),
        CheckConstraint("bathrooms >= 0", name="chk_bathrooms_non_negative"),
        CheckConstraint("car_spaces >= 0", name="chk_car_spaces_non_negative"),
        Index("idx_properties_location_geom", "location_geom", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    normalized_address: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    street_address: Mapped[str] = mapped_column(String(255), nullable=False)
    suburb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("suburbs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    land_size_sqm: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    building_area_sqm: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    bedrooms: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    bathrooms: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    car_spaces: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    property_type: Mapped[str] = mapped_column(String(50), nullable=False)
    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    location_geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    suburb: Mapped["Suburb"] = relationship("Suburb", back_populates="properties")
    listings: Mapped[List["Listing"]] = relationship(
        "Listing", back_populates="property", cascade="all, delete-orphan"
    )
    settled_sales: Mapped[List["SettledSale"]] = relationship(
        "SettledSale", back_populates="property", cascade="all, delete-orphan"
    )


class Listing(Base):
    """Live and historical property campaign listing with agent guide and AVM metrics."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_listing_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="Domain/REA", nullable=False)
    first_listed_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="Active", nullable=False, index=True)
    price_guide_raw: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    price_guide_low: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    price_guide_high: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    agency_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    avm_estimated_value: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    underquoting_gap_pct: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    underquoting_risk_level: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    property: Mapped["Property"] = relationship("Property", back_populates="listings")
    price_revisions: Mapped[List["ListingPriceRevision"]] = relationship(
        "ListingPriceRevision", back_populates="listing", cascade="all, delete-orphan"
    )


class ListingPriceRevision(Base):
    """Timestamped log capturing every price cut or guide revision on a listing."""

    __tablename__ = "listing_price_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    previous_price_low: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    previous_price_high: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    new_price_low: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    new_price_high: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    price_change_pct: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    revision_reason: Mapped[Optional[str]] = mapped_column(
        String(120), default="Vendor Price Adjustment", nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="price_revisions")


class SettledSale(Base):
    """Valuer-General settled historical sales record."""

    __tablename__ = "settled_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sold_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    sale_method: Mapped[str] = mapped_column(String(50), default="Auction", nullable=False)
    days_on_market: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    source_reference: Mapped[Optional[str]] = mapped_column(
        String(100), default="Valuer-General Victoria", nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    property: Mapped["Property"] = relationship("Property", back_populates="settled_sales")


class DatabaseManager:
    """Manages SQLAlchemy engine creation, session lifecycle, and PostGIS queries."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or settings.database_url
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.database_url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                future=True,
            )
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
        return self._session_factory

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope around a series of database operations."""
        session: Session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def check_connection(self) -> bool:
        """Verify database connectivity and PostGIS availability."""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.debug("Database connection unavailable: %s", exc)
            return False

    def initialize_schema(self, schema_sql_path: Optional[Path] = None) -> None:
        """Execute the raw SQL PostGIS schema script to initialize tables and triggers."""
        path = schema_sql_path or (
            Path(__file__).resolve().parent.parent / "database" / "schema.sql"
        )
        sql_script = path.read_text(encoding="utf-8")
        with self.engine.begin() as conn:
            conn.execute(text(sql_script))
        logger.info("Successfully initialized PostGIS database schema from %s", path)

    @staticmethod
    def make_point_wkt(longitude: float, latitude: float) -> WKTElement:
        """Construct a GeoAlchemy2 WKTElement for an EPSG:4326 point."""
        return WKTElement(f"POINT({longitude} {latitude})", srid=4326)


db_manager = DatabaseManager()

