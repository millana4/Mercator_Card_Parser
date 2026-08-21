# -*- coding: utf-8 -*-
"""
run_worker.py — точка входа Redis-воркера.
Запуск: python -m app.worker.run_worker
"""
from app.core.logging import setup_logging, get_logger
from app.worker.consumer import TaskConsumer

logger = get_logger(__name__)


def main():
    setup_logging()
    logger.info("=== Запуск Ozon Redis-воркера ===")
    TaskConsumer().run()


if __name__ == "__main__":
    main()