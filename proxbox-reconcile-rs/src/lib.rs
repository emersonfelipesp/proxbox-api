use pyo3::prelude::*;

mod diff;
mod normalize;
mod vm;

#[pyfunction]
fn engine_version() -> &'static str {
    "0.1.0"
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(engine_version, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engine_version_matches_package_version() {
        assert_eq!(engine_version(), "0.1.0");
    }
}
