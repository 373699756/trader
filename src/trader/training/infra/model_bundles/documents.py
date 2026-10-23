"""Whitelisted model and prediction-report documents at the persistence boundary."""


def BuildIndustryModelDocument(models: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "industries": {
            industry: {
                key: value
                for key, value in parameters.items()
                if key not in {"training_rows", "validation_rows", "prediction_metrics"}
            }
            for industry, parameters in models.items()
        }
    }


def BuildPredictionMetricsDocument(models: dict[str, dict[str, object]]) -> dict[str, object]:
    return {industry: parameters["prediction_metrics"] for industry, parameters in models.items()}


__all__ = ["BuildIndustryModelDocument", "BuildPredictionMetricsDocument"]
