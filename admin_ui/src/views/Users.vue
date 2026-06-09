<template>
  <div>
    <h1 class="page-title">用户管理</h1>
    <p class="page-subtitle">admin 看到全部页面；普通用户只能登录 Chainlit 聊天。可以禁用账号。</p>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">+ 新增用户</el-button>
    </div>
    <el-table :data="rows" stripe>
      <el-table-column prop="username" label="账号" />
      <el-table-column prop="display_name" label="昵称" />
      <el-table-column prop="role" label="角色" width="100" />
      <el-table-column label="启用" width="80">
        <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</el-tag></template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.show" :title="dlg.id ? '编辑用户' : '新增用户'" width="480px">
      <el-form :model="dlg.form" label-width="80px">
        <el-form-item label="账号"><el-input v-model="dlg.form.username" :disabled="!!dlg.id" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="dlg.form.password" type="password" show-password :placeholder="dlg.id ? '留空表示不修改' : ''" /></el-form-item>
        <el-form-item label="昵称"><el-input v-model="dlg.form.display_name" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="dlg.form.role">
            <el-option label="管理员" value="admin" />
            <el-option label="普通用户" value="user" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="dlg.id" label="启用"><el-switch v-model="dlg.form.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button type="primary" @click="onSubmit">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const dlg = ref({ show: false, id: null, form: { username: '', password: '', display_name: '', role: 'user', enabled: true } })

async function load() { rows.value = (await api.get('/users')).data }
onMounted(load)

function openCreate() { dlg.value = { show: true, id: null, form: { username: '', password: '', display_name: '', role: 'user', enabled: true } } }
function openEdit(row) { dlg.value = { show: true, id: row.id, form: { ...row, password: '' } } }

async function onSubmit() {
  try {
    if (dlg.value.id) {
      const p = { ...dlg.value.form }
      if (!p.password) delete p.password
      delete p.username
      await api.patch(`/users/${dlg.value.id}`, p)
    } else {
      await api.post('/users', dlg.value.form)
    }
    ElMessage.success('已保存'); dlg.value.show = false; await load()
  } catch (e) { ElMessage.error(e.response?.data?.error || '保存失败') }
}
async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除用户 ${row.username}？`, '提示', { type: 'warning' })
  await api.delete(`/users/${row.id}`); await load()
}
</script>
