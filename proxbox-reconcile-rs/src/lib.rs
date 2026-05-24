use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod diff;
mod normalize;
mod vm;

#[pyfunction]
fn engine_version() -> &'static str {
    "0.1.0"
}

#[pyfunction]
fn build_vm_operation_queue_json(py: Python<'_>, input: Vec<u8>) -> PyResult<Vec<u8>> {
    py.detach(|| {
        vm::build_vm_operation_queue_json(&input)
            .map_err(|error| PyValueError::new_err(error.to_string()))
    })
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(engine_version, m)?)?;
    m.add_function(wrap_pyfunction!(build_vm_operation_queue_json, m)?)?;
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
