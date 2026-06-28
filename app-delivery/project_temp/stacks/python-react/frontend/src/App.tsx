import { ConfigProvider, Layout, Typography } from 'antd'

import { theme } from './styles/theme'

const { Content } = Layout
const { Paragraph, Title } = Typography

export function App() {
  return (
    <ConfigProvider theme={theme}>
      <Layout style={{ minHeight: '100vh' }}>
        <Content style={{ display: 'grid', placeItems: 'center', padding: 32 }}>
          <main>
            <Title>{{PROJECT_NAME}}</Title>
            <Paragraph>OPC project initialized.</Paragraph>
          </main>
        </Content>
      </Layout>
    </ConfigProvider>
  )
}